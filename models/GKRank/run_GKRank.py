import os
import sys
import numpy as np
from time import time

from helper import load_from_json, write_to_json
from scene_similarity_models import GraphKernel


def apply_gk(query_dict, queries, voxel_dir, scene_graph_dir, nth_closest_dict, walk_length, mode):
    query_results = {}
    for query in queries:
        if query in visited:
            continue
        visited.add(query)
        query_scene_name = query_dict[query]['example']['scene_name']
        gk_model = GraphKernel(voxel_dir=voxel_dir, graph_dir=scene_graph_dir, graph1_name=query_scene_name,
                               nth_closest_dict=nth_closest_dict, mode=mode, walk_length=walk_length)
        # run GKRank and find the ranked results
        t = time()
        query_node = query_dict[query]['example']['query']
        context_nodes = query_dict[query]['example']['context_objects']
        target_graphs = os.listdir(os.path.join(scene_graph_dir, mode))
        # exclude the current query graph from the target graphs
        target_graphs.remove(query_scene_name)
        ranked_results = gk_model.context_based_subgraph_matching(query_node, context_nodes, target_graphs)
        query_duration = time() - t
        print('Search query took {} minutes '.format(round(query_duration / 60, 2)))

        # add the ranked results as target subscenes
        query_results[query] = query_dict[query]
        target_subscenes = []
        for kernel, scene_name, target_node, correspondence in ranked_results:
            target_subscene = {'scene_name': scene_name, 'target': target_node, 'correspondence': correspondence,
                               'context_match': len(correspondence), 'kernel': kernel}
            target_subscenes.append(target_subscene)

        # sort the results using the number of correspondences and then the avg kernel
        target_subscenes = sorted(target_subscenes, reverse=True, key=lambda x: (x['context_match'], x['kernel']))
        query_results[query]['target_subscenes'] = target_subscenes

    return query_results


def main(num_chunks, chunk_idx, action='extract_target_subscenes'):
    if action == 'extract_target_subscenes':
        # define the parameters for running gk type models
        query_dict_input_path = '../../queries/matterport3d/{}/query_dict_objects_top10.json'.format(mode)
        query_dict_output_path = '../../results/matterport3d/GKRank/query_dict_objects_{}_{}.json'.format(mode, chunk_idx)
        voxel_dir = '../../results/matterport3d/GKRank/voxels'
        scene_graph_dir = '../../results/matterport3d/GKRank/scene_graphs'
        nth_closest_dict = load_from_json('../../results/matterport3d/GKRank/nth_closest_obj.json')
        walk_length = 3

        # load the input query dict
        query_dict = load_from_json(query_dict_input_path)

        # run GKRank on the queries in parallel
        chunk_size = int(np.ceil(len(query_dict.keys()) / num_chunks))
        queries = sorted(query_dict.keys())
        query_results = apply_gk(query_dict, queries[chunk_idx * chunk_size: (chunk_idx + 1) * chunk_size], voxel_dir,
                                 scene_graph_dir, nth_closest_dict, walk_length, mode)

        # save the query results
        write_to_json(query_results, query_dict_output_path)

    if action == 'combine_query_results':
        query_results = {}
        gk_dir = '../../results/matterport3d/GKRank'
        file_names = os.listdir(gk_dir)
        for file_name in file_names:
            # make sure the file name is a chunk of the original query dict
            # TODO: change the split to 5 if object queries, otherwise to 4.
            if ('query_dict' in file_name) and (mode in file_name) and len(file_name.split('_')) == 5:
                curr_query_results = load_from_json(os.path.join(gk_dir, file_name))
                for q, q_info in curr_query_results.items():
                    query_results[q] = q_info

        # save the combined query results
        write_to_json(query_results, os.path.join(gk_dir, 'query_dict_objects_top10_{}_{}.json'.format(mode, experiment_name)))


if __name__ == '__main__':
    # set mode to test or val
    mode = 'test'
    experiment_name = 'GKRank'

    visited = set()
    if len(sys.argv) == 1:
        main(1, 0, 'extract_target_subscenes')
    elif len(sys.argv) == 2:
        main(1, 0, sys.argv[1])
    else:
        # To run in parallel you can use the command:
        # parallel -j5 "python3 -u run_GKRank.py {1} {2} {3}" ::: 5 ::: 0 1 2 3 4 ::: extract_target_subscenes
        main(int(sys.argv[1]), int(sys.argv[2]), sys.argv[3])
