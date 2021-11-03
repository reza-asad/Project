import os
from optparse import OptionParser
from time import time
import torch
from torch.utils.data import DataLoader

from region_dataset import Region
from transformer_models import PointTransformerCls
from scripts.helper import load_from_json, write_to_json
from train_point_transformer import evaluate_net

alpha = 1
gamma = 2.


def run_classifier(cat_to_idx, args):
    # create the training dataset
    dataset = Region(args.mesh_dir, args.scene_dir, args.metadata_path, args.accepted_cats_path,
                     num_local_crops=0, num_global_crops=0, mode=args.mode, cat_to_idx=cat_to_idx, num_files=None,
                     num_points=args.num_point)

    # create the dataloaders
    loader = DataLoader(dataset, batch_size=args.batch_size, num_workers=4, shuffle=True)

    # load the model
    classifier = PointTransformerCls(args)
    classifier = torch.nn.DataParallel(classifier).cuda()
    checkpoint = torch.load(os.path.join(args.cp_dir, args.best_model_name))
    classifier.load_state_dict(checkpoint['model_state_dict'])
    classifier.eval()

    # evaluate the model
    validation_loss, per_class_accuracy = evaluate_net(classifier, loader, cat_to_idx)

    # save the per class accuracies.
    per_class_accuracy_final = {}
    for c, (num_correct, num_total) in per_class_accuracy.items():
        if num_total != 0:
            accuracy = float(num_correct) / num_total
            per_class_accuracy_final[c] = accuracy
    write_to_json(per_class_accuracy_final, os.path.join(args.cp_dir, 'per_class_accuracy.json'))


def get_args():
    parser = OptionParser()
    parser.add_option('--dataset', default='scannet')
    parser.add_option('--mode', dest='mode', default='val')
    parser.add_option('--accepted_cats_path', dest='accepted_cats_path',
                      default='../../data/{}/accepted_cats.json')
    parser.add_option('--mesh_dir', dest='mesh_dir',
                      default='../../data/{}/mesh_regions')
    parser.add_option('--scene_dir', dest='scene_dir', default='../../data/{}/scenes')
    parser.add_option('--metadata_path', dest='metadata_path', default='../../data/{}/metadata.csv')
    parser.add_option('--cp_dir', dest='cp_dir',
                      default='../../results/{}/LearningBased/region_classification_transformer_4_64_1024')
    # TODO: change this to the best
    parser.add_option('--best_model_name', dest='best_model_name', default='CP_best_fixed_region.pth')

    parser.add_option('--num_point', dest='num_point', default=1024, type='int')
    parser.add_option('--nblocks', dest='nblocks', default=4, type='int')
    parser.add_option('--nneighbor', dest='nneighbor', default=16, type='int')
    parser.add_option('--input_dim', dest='input_dim', default=3, type='int')
    parser.add_option('--transformer_dim', dest='transformer_dim', default=64, type='int')
    parser.add_option('--batch_size', dest='batch_size', default=140, type='int')

    (options, args) = parser.parse_args()
    return options


def adjust_paths(args, exceptions):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    for k, v in vars(args).items():
        if (type(v) is str) and ('/' in v) and k not in exceptions:
            v = v.format(args.dataset)
            vars(args)[k] = os.path.join(base_dir, v)


def main():
    # get the arguments
    args = get_args()
    adjust_paths(args, exceptions=[])

    # create a directory for checkpoints
    if not os.path.exists(args.cp_dir):
        os.makedirs(args.cp_dir)

    # prepare the accepted categories for training.
    accepted_cats = load_from_json(args.accepted_cats_path)
    accepted_cats = sorted(accepted_cats)
    cat_to_idx = {cat: i for i, cat in enumerate(accepted_cats)}
    args.num_class = len(cat_to_idx)

    # time the training
    t = time()
    run_classifier(cat_to_idx, args)
    t2 = time()
    print("Testing took %.3f minutes" % ((t2 - t) / 60))


if __name__ == '__main__':
    main()



