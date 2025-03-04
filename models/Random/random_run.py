import os
from time import time
import numpy as np
import pandas as pd
from optparse import OptionParser

from scripts.helper import load_from_json, write_to_json
from scripts.box import Box
from scripts.iou import IoU


def map_cats_to_scene_objects(df):
    cat_to_scene_objects = {}
    cats = df['mpcat40'].values
    scene_objects = df[['room_name', 'objectId']].apply(lambda x: '-'.join([x['room_name'], str(x['objectId'])]), axis=1).values
    for i, cat in enumerate(cats):
        if cat not in cat_to_scene_objects:
            cat_to_scene_objects[cat] = [scene_objects[i]]
        else:
            cat_to_scene_objects[cat].append(scene_objects[i])
    return cat_to_scene_objects


def map_cat_to_objects(scene, source_node):
    cat_to_objects = {}
    # map each category to its object id except for the source node
    for obj, node_info in scene.items():
        if obj != source_node:
            cat = node_info['category'][0]
            if cat not in cat_to_objects:
                cat_to_objects[cat] = [obj]
            else:
                cat_to_objects[cat].append(obj)
    return cat_to_objects


def RandomRank(query_info, model_names, scene_dir, mode, topk):
    # shuffle the objects in all scenes
    np.random.shuffle(model_names)

    # for each object sample context objects randomly from the object's scene
    num_subscenes = 0
    target_subscenes = []
    query_scene_name = query_info['example']['scene_name']
    num_context_objects = len(query_info['example']['context_objects'])
    model_idx = 0
    while num_subscenes < topk:
        model_name = model_names[model_idx]
        model_idx += 1
        target = model_name.split('-')[1].split('.')[0]
        scene_name = model_name.split('-')[0] + '.json'
        # exclude the scene if it is the query scene
        if query_scene_name == scene_name:
            continue
        num_subscenes += 1
        scene = load_from_json(os.path.join(scene_dir, mode, scene_name))
        all_except_target = [obj for obj in scene.keys() if obj != target]
        if num_context_objects <= len(all_except_target):
            sample_context = np.random.choice(all_except_target, num_context_objects, replace=False).tolist()
            num_sample = num_context_objects
        else:
            sample_context = all_except_target
            num_sample = len(sample_context)

        # assign a random correspondence between the sample and the query objects.
        correspondence = {}
        for i in range(num_sample):
            correspondence[sample_context[i]] = query_info['example']['context_objects'][i]

        subscene = {'scene_name': scene_name, 'target': target, 'correspondence': correspondence,
                    'context_match': len(correspondence)}
        target_subscenes.append(subscene)

    # sort the target subscenes based on the number of matching context objects
    target_subscenes = sorted(target_subscenes, reverse=True, key=lambda x: x['context_match'])

    return target_subscenes


def CatRank(query_info, query_mode, scene_dir, mode, cat_to_scene_objects, topk):
    # load the query scene and extract the category of the objects
    query_scene_name = query_info['example']['scene_name']
    query = query_info['example']['query']
    context_objects = query_info['example']['context_objects']
    query_scene = load_from_json(os.path.join(scene_dir, query_mode, query_scene_name))
    query_cat = query_scene[query]['category'][0]
    context_obj_to_cat = {obj: query_scene[obj]['category'][0] for obj in context_objects}

    # map the category of each context object to the objects having that category
    q_context_cat_to_objects = {}
    for obj, cat in context_obj_to_cat.items():
        if cat not in q_context_cat_to_objects:
            q_context_cat_to_objects[cat] = [obj]
        else:
            q_context_cat_to_objects[cat].append(obj)

    # for each target object that has the same category as query cat sample context objects
    target_objects = cat_to_scene_objects[query_cat]
    target_subscenes = []
    for target_object in target_objects:
        scene_name = target_object.split('-')[0] + '.json'
        # exclude the scene if it is the query scene
        if scene_name == query_scene_name:
            continue
        target_object = target_object.split('-')[-1]

        # load the scene and map its cats to the objects
        scene = load_from_json(os.path.join(scene_dir, mode, scene_name))
        t_cat_to_objects = map_cat_to_objects(scene, target_object)

        # for each context object in the query scene sample an object with the same category from the current scene.
        correspondence = {}
        for cat, objects in q_context_cat_to_objects.items():
            if cat in t_cat_to_objects:
                if len(objects) <= len(t_cat_to_objects[cat]):
                    sample = np.random.choice(t_cat_to_objects[cat], len(objects), replace=False).tolist()
                    num_sample = len(objects)
                else:
                    sample = t_cat_to_objects[cat]
                    num_sample = len(sample)

                # assign a random correspondence between the context objects in query and selected candidates in target.
                for i in range(num_sample):
                    correspondence[sample[i]] = objects[i]

        # populate the subscene attributes and save it.
        target_subscene = {'scene_name': scene_name, 'target': target_object, 'correspondence': correspondence,
                           'context_match': len(correspondence)}
        target_subscenes.append(target_subscene)

    # sort the target subscenes based on the number of matching context objects
    target_subscenes = sorted(target_subscenes, reverse=True, key=lambda x: x['context_match'])[:topk]

    return target_subscenes


def get_args():
    parser = OptionParser()
    parser.add_option('--mode', dest='mode', default='val', help='val|test')
    parser.add_option('--accepted_cats_path', default='../../data/matterport3d/accepted_cats_top10.json')
    parser.add_option('--metadata_path', default='../../data/matterport3d/metadata_non_equal_full_top10.csv')
    parser.add_option('--scene_dir', default='../../results/matterport3d/scenes_top10')
    parser.add_option('--query_dir', default='../../queries/matterport3d/')
    parser.add_option('--query_input_file_name', default='query_dict_val_top10.json')
    parser.add_option('--results_dir', default='../../results/matterport3d')
    parser.add_option('--model_name', dest='model_name', default='CatRank', help='RandomRank|CatRank')
    (options, args) = parser.parse_args()
    return options


def main():
    # get the arguments
    args = get_args()

    # define initial parameters
    query_dict_input_path = os.path.join(args.query_dir, args.query_input_file_name)
    results_dir = os.path.join(args.results_dir, args.model_name)
    if not os.path.exists(results_dir):
        os.mkdir(results_dir)

    query_dict_output_path = os.path.join(results_dir, 'query_dict_{}_top10_{}.json'.format(args.mode, args.model_name))
    accepted_cats = set(load_from_json(args.accepted_cats_path))
    topk = 100

    # read the query dict and the metadata
    query_dict = load_from_json(query_dict_input_path)
    df_metadata = pd.read_csv(args.metadata_path)
    df_metadata = df_metadata[df_metadata['split'] == args.mode]

    # filter the metadata to only include objects with accepted category
    cat_is_accepted = df_metadata['mpcat40'].apply(lambda x: x in accepted_cats)
    df_metadata = df_metadata.loc[cat_is_accepted]

    # map the categories to scene objects if necessary
    if args.model_name == 'RandomRank':
        model_names = df_metadata[['room_name', 'objectId']].apply(lambda x: '-'.join([x['room_name'],
                                                                                       str(x['objectId'])]), axis=1)
        model_names = model_names.values
        for query, query_info in query_dict.items():
            target_subscenes = RandomRank(query_info, model_names, args.scene_dir, args.mode, topk)
            query_info['target_subscenes'] = target_subscenes

    elif args.model_name == 'CatRank':
        cat_to_scene_objects = map_cats_to_scene_objects(df_metadata)
        for query, query_info in query_dict.items():
            target_subscenes = CatRank(query_info, args.mode, args.scene_dir, args.mode, cat_to_scene_objects, topk)
            query_info['target_subscenes'] = target_subscenes

    else:
        raise Exception('Model not defined')

    # test if for each query the query node is not in the context nodes
    for query, query_info in query_dict.items():
        for target_subscene in query_info['target_subscenes']:
            t = target_subscene['target']
            context_objects = target_subscene['correspondence'].keys()
            if t in context_objects:
                raise Exception('Target node was included in the context objects.')

    # save the changes to query dict
    write_to_json(query_dict, query_dict_output_path)


if __name__ == '__main__':
    t = time()
    main()
    duration = time() - t
    print('Running model took {} minutes'.format(round(duration / 60, 2)))
