# Copyright (c) Facebook, Inc. and its affiliates.
import numpy as np
import torch
import datetime
import logging
import math
import time
import sys

from torch.distributed.distributed_c10d import reduce
from utils.ap_calculator import APCalculator
from utils.misc import SmoothedValue
from utils.dist import (
    all_gather_dict,
    all_reduce_average,
    is_primary,
    reduce_dict,
    barrier,
)
import utils.pc_util as pc_util


def compute_learning_rate(args, curr_epoch_normalized):
    assert curr_epoch_normalized <= 1.0 and curr_epoch_normalized >= 0.0
    if (
        curr_epoch_normalized <= (args.warm_lr_epochs / args.max_epoch)
        and args.warm_lr_epochs > 0
    ):
        # Linear Warmup
        curr_lr = args.warm_lr + curr_epoch_normalized * args.max_epoch * (
            (args.base_lr - args.warm_lr) / args.warm_lr_epochs
        )
    else:
        # Cosine Learning Rate Schedule
        curr_lr = args.final_lr + 0.5 * (args.base_lr - args.final_lr) * (
            1 + math.cos(math.pi * curr_epoch_normalized)
        )
    return curr_lr


def adjust_learning_rate(args, optimizer, curr_epoch):
    curr_lr = compute_learning_rate(args, curr_epoch)
    for param_group in optimizer.param_groups:
        param_group["lr"] = curr_lr
    return curr_lr


def train_one_epoch(
    args,
    curr_epoch,
    model,
    optimizer,
    criterion,
    dataset_config,
    dataset_loader,
    logger,
):

    ap_calculator = APCalculator(
        dataset_config=dataset_config,
        ap_iou_thresh=[0.25, 0.5],
        class2type_map=dataset_config.class2type,
        exact_eval=False,
    )

    curr_iter = curr_epoch * len(dataset_loader)
    max_iters = args.max_epoch * len(dataset_loader)
    net_device = next(model.parameters()).device

    time_delta = SmoothedValue(window_size=10)
    loss_avg = SmoothedValue(window_size=10)

    model.train()
    barrier()

    for batch_idx, batch_data_label in enumerate(dataset_loader):
        curr_time = time.time()
        curr_lr = adjust_learning_rate(args, optimizer, curr_iter / max_iters)
        for key in batch_data_label:
            if key != 'scan_name':
                batch_data_label[key] = batch_data_label[key].to(net_device)

        # Forward pass
        optimizer.zero_grad()

        # TODO: add masked subscene inputs.
        query_point_cloud_masked = torch.cat((batch_data_label["point_clouds_with_mask"][..., 0:3],
                                              batch_data_label["point_clouds_with_mask"][..., 4:5]),
                                             dim=2)
        masked_subscene_inputs = {"point_clouds": query_point_cloud_masked.clone()}
        inputs = {
            "point_clouds": batch_data_label["point_clouds"],
            "point_cloud_dims_min": batch_data_label["point_cloud_dims_min"],
            "point_cloud_dims_max": batch_data_label["point_cloud_dims_max"],
        }

        # TODO: encode the point cloud with mask first.
        if not args.aggressive_rot:
            enc_xyz_q, enc_features_q = model(masked_subscene_inputs, is_masked=True, encoder_only=True)
            encoded_subscene_inputs = {
                "enc_xyz": enc_xyz_q,
                "enc_features": enc_features_q.transpose(1, 0)
            }
            outputs = model(inputs, encoded_subscene_inputs=encoded_subscene_inputs, is_masked=False)
        # TODO: 1) encode the target scene. 2) find the rotation between target and query. 3) rotate the target scene
        #  and compute features. 3) decode the aligned target conditioned on the query.
        else:
            # apply the encoder (without mask) on the query scene.
            subscene_inputs = {"point_clouds": batch_data_label["point_clouds_with_mask"][..., :4].clone()}
            enc_xyz_q, enc_features_q = model(subscene_inputs, is_masked=False, encoder_only=True)
            encoded_subscene_inputs = {
                "enc_xyz": enc_xyz_q,
                "enc_features": enc_features_q.transpose(1, 0)
            }

            # predict the rotation that best aligns target and query.
            pred_rot_mat = model(inputs, encoded_subscene_inputs=encoded_subscene_inputs, is_masked=False,
                                 predict_rotation=True)
            pred_rot_mat = pred_rot_mat.to(device=enc_features_q.device)

            # rotate the query point cloud to align with target using gt
            B, _, _ = enc_features_q.shape
            for i in range(B):
                masked_subscene_inputs['point_clouds'][i, :, 0:3] = torch.mm(masked_subscene_inputs['point_clouds'][i, :, 0:3],
                                                                             batch_data_label['rot_mat'].permute(0, 2, 1)[i, ...])

            # encode the aligned query scene.
            enc_xyz_q, enc_features_q = model(masked_subscene_inputs, is_masked=True, encoder_only=True)
            encoded_subscene_inputs = {
                "enc_xyz": enc_xyz_q,
                "enc_features": enc_features_q.transpose(1, 0)
            }

            # apply the model on aligned query and target.
            outputs = model(inputs, encoded_subscene_inputs, is_masked=False)
            outputs['outputs']['pred_rot_mat'] = pred_rot_mat

        # Compute loss
        loss, loss_dict = criterion(outputs, batch_data_label)
        loss_reduced = all_reduce_average(loss)
        loss_dict_reduced = reduce_dict(loss_dict)

        if not math.isfinite(loss_reduced.item()):
            logging.info(f"Loss in not finite. Training will be stopped.")
            sys.exit(1)

        loss.backward()
        if args.clip_gradient > 0:
            torch.nn.utils.clip_grad_norm_(model.parameters(), args.clip_gradient)
        optimizer.step()

        if curr_iter % args.log_metrics_every == 0:
            # This step is slow. AP is computed approximately and locally during training.
            # It will gather outputs and ground truth across all ranks.
            # It is memory intensive as point_cloud ground truth is a large tensor.
            # If GPU memory is not an issue, uncomment the following lines.
            # outputs["outputs"] = all_gather_dict(outputs["outputs"])
            # batch_data_label = all_gather_dict(batch_data_label)
            ap_calculator.step_meter(outputs, batch_data_label)

        time_delta.update(time.time() - curr_time)
        loss_avg.update(loss_reduced.item())

        # logging
        if is_primary() and curr_iter % args.log_every == 0:
            mem_mb = torch.cuda.max_memory_allocated() / (1024 ** 2)
            eta_seconds = (max_iters - curr_iter) * time_delta.avg
            eta_str = str(datetime.timedelta(seconds=int(eta_seconds)))
            print(
                f"Epoch [{curr_epoch}/{args.max_epoch}]; Iter [{curr_iter}/{max_iters}]; Loss {loss_avg.avg:0.2f}; LR {curr_lr:0.2e}; Iter time {time_delta.avg:0.2f}; ETA {eta_str}; Mem {mem_mb:0.2f}MB"
            )
            logger.log_scalars(loss_dict_reduced, curr_iter, prefix="Train_details/")

            train_dict = {}
            train_dict["lr"] = curr_lr
            train_dict["memory"] = mem_mb
            train_dict["loss"] = loss_avg.avg
            train_dict["batch_time"] = time_delta.avg
            logger.log_scalars(train_dict, curr_iter, prefix="Train/")

        curr_iter += 1
        barrier()

    return ap_calculator


@torch.no_grad()
def evaluate(
    args,
    curr_epoch,
    model,
    criterion,
    dataset_config,
    dataset_loader,
    logger,
    curr_train_iter,
    per_class_proposal=True
):

    # ap calculator is exact for evaluation. This is slower than the ap calculator used during training.
    ap_calculator = APCalculator(
        dataset_config=dataset_config,
        ap_iou_thresh=[0.25, 0.5],
        class2type_map=dataset_config.class2type,
        exact_eval=True,
        per_class_proposal=per_class_proposal
    )

    curr_iter = 0
    net_device = next(model.parameters()).device
    num_batches = len(dataset_loader)

    time_delta = SmoothedValue(window_size=10)
    loss_avg = SmoothedValue(window_size=10)
    model.eval()
    barrier()
    epoch_str = f"[{curr_epoch}/{args.max_epoch}]" if curr_epoch > 0 else ""

    for batch_idx, batch_data_label in enumerate(dataset_loader):
        curr_time = time.time()
        for key in batch_data_label:
            if key != 'scan_name':
                batch_data_label[key] = batch_data_label[key].to(net_device)

        # TODO: add masked subscene inputs.
        query_point_cloud_masked = torch.cat((batch_data_label["point_clouds_with_mask"][..., 0:3],
                                              batch_data_label["point_clouds_with_mask"][..., 4:5]),
                                             dim=2)
        masked_subscene_inputs = {"point_clouds": query_point_cloud_masked.clone()}
        inputs = {
            "point_clouds": batch_data_label["point_clouds"],
            "point_cloud_dims_min": batch_data_label["point_cloud_dims_min"],
            "point_cloud_dims_max": batch_data_label["point_cloud_dims_max"],
        }

        # TODO: encode the point cloud with mask first.
        if not args.aggressive_rot:
            enc_xyz_q, enc_features_q = model(masked_subscene_inputs, is_masked=True, encoder_only=True)
            encoded_subscene_inputs = {
                "enc_xyz": enc_xyz_q,
                "enc_features": enc_features_q.transpose(1, 0)
            }
            outputs = model(inputs, encoded_subscene_inputs=encoded_subscene_inputs, is_masked=False)
        # TODO: 1) encode the target scene. 2) find the rotation between target and query. 3) rotate the target scene
        #  and compute features. 3) decode the aligned target conditioned on the query.
        else:
            # apply the encoder (without mask) on the query scene.
            subscene_inputs = {"point_clouds": batch_data_label["point_clouds_with_mask"][..., :4].clone()}
            enc_xyz_q, enc_features_q = model(subscene_inputs, is_masked=False, encoder_only=True)
            encoded_subscene_inputs = {
                "enc_xyz": enc_xyz_q,
                "enc_features": enc_features_q.transpose(1, 0)
            }

            # predict the rotation that best aligns target and query.
            pred_rot_mat = model(inputs, encoded_subscene_inputs=encoded_subscene_inputs, is_masked=False,
                                 predict_rotation=True)
            pred_rot_mat = pred_rot_mat.to(device=enc_features_q.device)

            # rotate the query point cloud to align with target using gt
            B, _, _ = enc_features_q.shape
            for i in range(B):
                masked_subscene_inputs['point_clouds'][i, :, 0:3] = torch.mm(masked_subscene_inputs['point_clouds'][i, :, 0:3],
                                                                             pred_rot_mat.permute(0, 2, 1)[i, ...])

            # encode the aligned query scene.
            enc_xyz_q, enc_features_q = model(masked_subscene_inputs, is_masked=True, encoder_only=True)
            encoded_subscene_inputs = {
                "enc_xyz": enc_xyz_q,
                "enc_features": enc_features_q.transpose(1, 0)
            }

            # apply the model on aligned query and target.
            outputs = model(inputs, encoded_subscene_inputs, is_masked=False)
            outputs['outputs']['pred_rot_mat'] = pred_rot_mat

        # Compute loss
        loss_str = ""
        if criterion is not None:
            loss, loss_dict = criterion(outputs, batch_data_label)

            loss_reduced = all_reduce_average(loss)
            loss_dict_reduced = reduce_dict(loss_dict)
            loss_avg.update(loss_reduced.item())
            loss_str = f"Loss {loss_avg.avg:0.2f};"

        # Memory intensive as it gathers point cloud GT tensor across all ranks
        outputs["outputs"] = all_gather_dict(outputs["outputs"])
        batch_data_label = all_gather_dict(batch_data_label)
        ap_calculator.step_meter(outputs, batch_data_label, with_rot_mat=args.aggressive_rot and args.augment_eval)
        time_delta.update(time.time() - curr_time)
        if is_primary() and curr_iter % args.log_every == 0:
            mem_mb = torch.cuda.max_memory_allocated() / (1024 ** 2)
            print(
                f"Evaluate {epoch_str}; Batch [{curr_iter}/{num_batches}]; {loss_str} Iter time {time_delta.avg:0.2f}; Mem {mem_mb:0.2f}MB"
            )

            test_dict = {}
            test_dict["memory"] = mem_mb
            test_dict["batch_time"] = time_delta.avg
            if criterion is not None:
                test_dict["loss"] = loss_avg.avg
        curr_iter += 1
        barrier()
    if is_primary():
        if criterion is not None:
            logger.log_scalars(
                loss_dict_reduced, curr_train_iter, prefix="Test_details/"
            )
        logger.log_scalars(test_dict, curr_train_iter, prefix="Test/")

    return ap_calculator


# TODO: add function to train the alignment module for one epoch.
def train_one_epoch_alignment(
    args,
    curr_epoch,
    model,
    optimizer,
    criterion,
    dataset_loader,
    logger,
):

    curr_iter = curr_epoch * len(dataset_loader)
    max_iters = args.max_epoch * len(dataset_loader)
    net_device = next(model.parameters()).device

    time_delta = SmoothedValue(window_size=10)
    loss_avg = SmoothedValue(window_size=10)

    model.train()
    barrier()

    total_training_loss = 0
    for batch_idx, batch_data_label in enumerate(dataset_loader):
        curr_time = time.time()
        curr_lr = adjust_learning_rate(args, optimizer, curr_iter / max_iters)
        for key in batch_data_label:
            if key != 'scan_name':
                batch_data_label[key] = batch_data_label[key].to(net_device)

        # Forward pass
        optimizer.zero_grad()

        inputs = {
            "query_point_clouds": batch_data_label["point_clouds_with_mask"][..., :4],
            "target_point_clouds": batch_data_label["point_clouds"]
        }
        outputs = model(inputs)
        outputs['pred_rot_mat'] = outputs['pred_rot_mat'].to(device=net_device)

        # Compute loss
        loss, loss_dict = criterion(outputs, batch_data_label)
        loss_reduced = all_reduce_average(loss)
        total_training_loss += loss_reduced
        loss_dict_reduced = reduce_dict(loss_dict)

        if not math.isfinite(loss_reduced.item()):
            logging.info(f"Loss in not finite. Training will be stopped.")
            sys.exit(1)

        loss.backward()
        if args.clip_gradient > 0:
            torch.nn.utils.clip_grad_norm_(model.parameters(), args.clip_gradient)
        optimizer.step()

        time_delta.update(time.time() - curr_time)
        loss_avg.update(loss_reduced.item())

        # logging
        if is_primary() and curr_iter % args.log_every == 0:
            mem_mb = torch.cuda.max_memory_allocated() / (1024 ** 2)
            eta_seconds = (max_iters - curr_iter) * time_delta.avg
            eta_str = str(datetime.timedelta(seconds=int(eta_seconds)))
            print(
                f"Epoch [{curr_epoch}/{args.max_epoch}]; Iter [{curr_iter}/{max_iters}]; Loss {loss_avg.avg:0.2f}; LR {curr_lr:0.2e}; Iter time {time_delta.avg:0.2f}; ETA {eta_str}; Mem {mem_mb:0.2f}MB"
            )
            logger.log_scalars(loss_dict_reduced, curr_iter, prefix="Train_details/")

            train_dict = {}
            train_dict["lr"] = curr_lr
            train_dict["memory"] = mem_mb
            train_dict["loss"] = loss_avg.avg
            train_dict["batch_time"] = time_delta.avg
            logger.log_scalars(train_dict, curr_iter, prefix="Train/")

        curr_iter += 1
        barrier()

    if is_primary():
        # print some predictions
        print(outputs['pred_rot_mat'][0, 0, 0].item(), batch_data_label['rot_mat'][0, 0, 0].item())
        print(outputs['pred_rot_mat'][0, 1, 0].item(), batch_data_label['rot_mat'][0, 1, 0].item())

    return total_training_loss


# TODO: add function to evaluate the alignment module after n epochs.
def evaluate_alignment(
    args,
    curr_epoch,
    model,
    criterion,
    dataset_loader,
    logger,
    curr_train_iter,
):

    curr_iter = 0
    net_device = next(model.parameters()).device
    num_batches = len(dataset_loader)

    time_delta = SmoothedValue(window_size=10)
    loss_avg = SmoothedValue(window_size=10)
    model.eval()
    barrier()
    epoch_str = f"[{curr_epoch}/{args.max_epoch}]" if curr_epoch > 0 else ""

    total_validation_loss = 0
    errors = []
    for batch_idx, batch_data_label in enumerate(dataset_loader):
        curr_time = time.time()
        for key in batch_data_label:
            if key != 'scan_name':
                batch_data_label[key] = batch_data_label[key].to(net_device)

        inputs = {
            "query_point_clouds": batch_data_label["point_clouds_with_mask"][..., :4],
            "target_point_clouds": batch_data_label["point_clouds"]
        }
        outputs = model(inputs)
        outputs['pred_rot_mat'] = outputs['pred_rot_mat'].to(device=net_device)

        # convert rotation matrices to angles.
        pred_angle = pc_util.find_angle_from_mat(outputs['pred_rot_mat'])
        gt_angle = pc_util.find_angle_from_mat(batch_data_label['rot_mat'])

        # compute and accumulate error.
        abs_diff = np.abs(pred_angle - gt_angle)
        batch_errors = np.minimum(abs_diff, 2*np.pi - abs_diff)
        for i in range(len(batch_errors)):
            errors.append(batch_errors[i])

        # Compute loss
        loss_str = ""
        if criterion is not None:
            loss, loss_dict = criterion(outputs, batch_data_label)

            loss_reduced = all_reduce_average(loss)
            total_validation_loss += loss_reduced.item()
            loss_dict_reduced = reduce_dict(loss_dict)
            loss_avg.update(loss_reduced.item())
            loss_str = f"Loss {loss_avg.avg:0.2f};"

        time_delta.update(time.time() - curr_time)
        if is_primary() and curr_iter % args.log_every == 0:
            mem_mb = torch.cuda.max_memory_allocated() / (1024 ** 2)
            print(
                f"Evaluate {epoch_str}; Batch [{curr_iter}/{num_batches}]; {loss_str} Iter time {time_delta.avg:0.2f}; Mem {mem_mb:0.2f}MB"
            )

            test_dict = {}
            test_dict["memory"] = mem_mb
            test_dict["batch_time"] = time_delta.avg
            if criterion is not None:
                test_dict["loss"] = loss_avg.avg
        curr_iter += 1
        barrier()
    if is_primary():
        if criterion is not None:
            logger.log_scalars(
                loss_dict_reduced, curr_train_iter, prefix="Test_details/"
            )
        logger.log_scalars(test_dict, curr_train_iter, prefix="Test/")

        # print some predictions
        print(outputs['pred_rot_mat'][0, 0, 0].item(), batch_data_label['rot_mat'][0, 0, 0].item())
        print(outputs['pred_rot_mat'][0, 1, 0].item(), batch_data_label['rot_mat'][0, 1, 0].item())

    return total_validation_loss, errors


# TODO: use svd for alignment
def evaluate_alignment_svd(
    args,
    curr_epoch,
    model,
    criterion,
    dataset_loader,
    logger,
    curr_train_iter,
):

    curr_iter = 0
    net_device = next(model.parameters()).device
    num_batches = len(dataset_loader)

    time_delta = SmoothedValue(window_size=10)
    loss_avg = SmoothedValue(window_size=10)
    model.eval()
    barrier()
    epoch_str = f"[{curr_epoch}/{args.max_epoch}]" if curr_epoch > 0 else ""

    errors = []
    for batch_idx, batch_data_label in enumerate(dataset_loader):
        curr_time = time.time()
        for key in batch_data_label:
            if key != 'scan_name':
                batch_data_label[key] = batch_data_label[key].to(net_device)

        inputs = {
            "query_point_clouds": batch_data_label["point_clouds_with_mask"][..., :3],
            "target_point_clouds": batch_data_label["point_clouds"][..., :3]
        }

        # apply svd
        outputs = {}
        B = len(batch_data_label['point_clouds'])
        pred_rot_mat = torch.zeros((B, 3, 3), dtype=torch.float32)
        for i in range(B):
            R, error = pc_util.svd_rotation(inputs['query_point_clouds'][i], inputs['target_point_clouds'][i])
            rotation = np.zeros((3, 3), dtype=np.float32)
            rotation[:2, :2] = R
            rotation[2, 2] = 1.0
            pred_rot_mat[i, ...] = torch.from_numpy(rotation)

        outputs['pred_rot_mat'] = pred_rot_mat
        outputs['pred_rot_mat'] = outputs['pred_rot_mat'].to(device=net_device)

        # convert rotation matrices to angles.
        pred_angle = pc_util.find_angle_from_mat(outputs['pred_rot_mat'])
        gt_angle = pc_util.find_angle_from_mat(batch_data_label['rot_mat'])

        # compute and accumulate error.
        abs_diff = np.abs(pred_angle - gt_angle)
        batch_errors = np.minimum(abs_diff, 2*np.pi - abs_diff)
        for i in range(len(batch_errors)):
            errors.append(batch_errors[i])

        time_delta.update(time.time() - curr_time)
        if is_primary() and curr_iter % args.log_every == 0:
            mem_mb = torch.cuda.max_memory_allocated() / (1024 ** 2)
            print(
                f"Evaluate {epoch_str}; Batch [{curr_iter}/{num_batches}]; Iter time {time_delta.avg:0.2f}; Mem {mem_mb:0.2f}MB"
            )

            test_dict = {}
            test_dict["memory"] = mem_mb
            test_dict["batch_time"] = time_delta.avg
        curr_iter += 1
        barrier()
    if is_primary():
        logger.log_scalars(test_dict, curr_train_iter, prefix="Test/")

        # print some predictions
        print(outputs['pred_rot_mat'][0, 0, 0].item(), batch_data_label['rot_mat'][0, 0, 0].item())
        print(outputs['pred_rot_mat'][0, 1, 0].item(), batch_data_label['rot_mat'][0, 1, 0].item())

    return errors
