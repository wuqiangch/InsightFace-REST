import os
import logging
import json
import shutil
from typing import List

import onnx
from onnx import checker
import mxnet as mx
import numpy as np

from configs import Configs
# Load edited version of mxnet.contrib.onnx
from mx2onnx_conv import onnx as onnx_mxnet
from remove_initializer_from_input import remove_initializer_from_input
from utils.model_store import get_model_file

print('mxnet version:', mx.__version__)
print('onnx version:', onnx.__version__)



# assert onnx.__version__ == '1.2.1'


# Based on
# https://github.com/onnx/models/issues/156#issuecomment-690847276
# And MXNet export _op_translations fixes from https://github.com/zheshipinyinMc/arcface_retinaface_mxnet2onnx

def add_dyn_batch(model_in, model_out):
    model = onnx.load(model_in)
    d = model.graph.input[0].type.tensor_type.shape.dim
    d[0].dim_value = -1
    for output in model.graph.output:
        d = output.type.tensor_type.shape.dim
        d[0].dim_value = -1
    onnx.save_model(model, model_out)


def mxnet_fixgamma_params(input_param: str, layers: List[str]):
    '''
    Replace gamma weights with zeros if fix_gamma is True.
    Specific to retinaface_mnet025_v*  and genderage_v1 models.

    :param input_param: path to MXnet .param file
    :param layers: List of nodes names containg fix_gamma = True attribute
    '''

    net_param = mx.nd.load(input_param)
    for layer in layers:
        name = f'arg:{layer}'
        gamma = net_param[name].asnumpy()
        gamma *= 0
        gamma += 1
        net_param[name] = mx.nd.array(gamma)
    return net_param


def mxnet_model_fix(input_symbol_path: str, input_params_path: str, rewrite: bool = True):
    '''
    Apply retinaface specific fixes, like renaming SoftmaxActivation and fixing gamma values.
    
    :param input_symbol_path: Path to MXNet .symbol file
    :param input_params_path: Path to MXNet .param file
    :param rewrite: Write fixed symbol and param at input path
    :return: 
    '''

    names = []
    fix_gamma_layers = []

    with open(input_symbol_path, 'r') as _input_symbol:
        fixed_sym = json.load(_input_symbol)
        for e in fixed_sym['nodes']:
            if e['op'] == 'SoftmaxActivation':
                e['op'] = 'softmax'
                e['attrs'] = {"axis": "1"}
            # Fix for "Graph must be in single static assignment (SSA) form"
            if e['name'] in names:
                e['name'] = f"{e['name']}_1"
            names.append(e['name'])
            if e.get('attrs', {}).get('fix_gamma') == 'True' and e['name'].endswith('_gamma'):
                fix_gamma_layers.append(e['name'])
        _input_symbol.close()

    fixed_params = mxnet_fixgamma_params(input_params_path, layers=fix_gamma_layers)

    if rewrite is True:
        mx.nd.save(input_params_path, fixed_params)
        with open(input_symbol_path, 'w') as sym_temp:
            json.dump(fixed_sym, sym_temp, indent=2)

    return fixed_sym, fixed_params


def arcface_onnx_fixes(onnx_path: str, rewrite: bool = True):
    '''
    Apply fixes specific for InsightFace ArcFace model.
    (BatchNormalization spatial, and PRelu reshape)

    :param onnx_path: Path to ONNX model produced by MXNet export (str)
    :param write: Overwrite input model (bool, default: True)
    :return: ONNX model object
    '''

    model = onnx.load(onnx_path)
    onnx_processed_nodes = []
    onnx_processed_inputs = []
    onnx_processed_outputs = []
    onnx_processed_initializers = []

    reshape_node = []

    for ind, node in enumerate(model.graph.node):
        if node.op_type == "PRelu":
            input_node = node.input
            input_bn = input_node[0]
            input_relu_gamma = input_node[1]
            output_node = node.output[0]

            input_reshape_name = "reshape{}".format(ind)
            slope_number = "slope{}".format(ind)

            node_reshape = onnx.helper.make_node(
                op_type="Reshape",
                inputs=[input_relu_gamma, input_reshape_name],
                outputs=[slope_number],
                name=slope_number
            )

            reshape_node.append(input_reshape_name)
            node_relu = onnx.helper.make_node(
                op_type="PRelu",
                inputs=[input_bn, slope_number],
                outputs=[output_node],
                name=output_node
            )
            onnx_processed_nodes.extend([node_reshape, node_relu])

        else:
            # If "spatial = 0" does not work for "BatchNormalization", change "spatial=1"
            # else comment this "if" condition
            if node.op_type == "BatchNormalization":
                for attr in node.attribute:
                    if (attr.name == "spatial"):
                        attr.i = 1
            onnx_processed_nodes.append(node)

    list_new_inp = []
    list_new_init = []
    for name_rs in reshape_node:
        new_inp = onnx.helper.make_tensor_value_info(
            name=name_rs,
            elem_type=onnx.TensorProto.INT64,
            shape=[4]
        )
        new_init = onnx.helper.make_tensor(
            name=name_rs,
            data_type=onnx.TensorProto.INT64,
            dims=[4],
            vals=[1, -1, 1, 1]
        )

        list_new_inp.append(new_inp)
        list_new_init.append(new_init)

    for k, inp in enumerate(model.graph.input):
        if "relu0_gamma" in inp.name or "relu1_gamma" in inp.name:  # or "relu_gamma" in inp.name:
            new_reshape = list_new_inp.pop(0)
            onnx_processed_inputs.extend([inp, new_reshape])
        else:
            onnx_processed_inputs.extend([inp])

    for k, outp in enumerate(model.graph.output):
        onnx_processed_outputs.extend([outp])

    for k, init in enumerate(model.graph.initializer):
        if "relu0_gamma" in init.name or "relu1_gamma" in init.name:
            new_reshape = list_new_init.pop(0)
            onnx_processed_initializers.extend([init, new_reshape])
        else:
            onnx_processed_initializers.extend([init])

    graph = onnx.helper.make_graph(
        onnx_processed_nodes,
        "mxnet_converted_model",
        onnx_processed_inputs,
        onnx_processed_outputs
    )

    graph.initializer.extend(onnx_processed_initializers)

    # Check graph
    checker.check_graph(graph)

    onnx_model = onnx.helper.make_model(graph)

    if rewrite:
        with open(onnx_path, "wb") as file_handle:
            serialized = onnx_model.SerializeToString()
            file_handle.write(serialized)
    return onnx_model


def convert_insight_model(symbol, params, onnx_path, input_shape=(1, 3, 112, 112)):

    output_dir = os.path.dirname(onnx_path)

    print("  Creating intermediate copy of source model...")

    intermediate_symbol = os.path.join(output_dir, 'symbol_fixed-symbol.json')
    intermediate_params = os.path.join(output_dir, 'symbol_fixed-0000.params')
    shutil.copy2(symbol, intermediate_symbol)
    shutil.copy2(params, intermediate_params)

    print("  Applying RetinaFace specific fixes to input MXNet model before conversion...")
    mxnet_model_fix(intermediate_symbol, intermediate_params, rewrite=True)

    print("  Exporting to ONNX...")
    onnx_mxnet.export_model(intermediate_symbol, intermediate_params, [input_shape], np.float32, onnx_path)

    print("  Applying ArcFace specific fixes to output ONNX")
    arcface_onnx_fixes(onnx_path, rewrite=True)

    print('  Removing initializer from inputs in ONNX model...')
    remove_initializer_from_input(onnx_path, onnx_path)

    print('  Removing intermediate *.symbol and *.params')
    os.remove(intermediate_symbol)
    os.remove(intermediate_params)

if __name__ == '__main__':

    config = Configs(models_dir='/models')

    models_to_convert = [name for name in config.mxnet_models if config.in_official_package(name)]
    custom_shape = (1, 3, 480, 640)

    for model in models_to_convert:
        print(f"Downloading model: {model}...")
        get_model_file(model, root=config.mxnet_models_dir)

    for model in models_to_convert:
        mxnet_symbol, mxnet_params = config.get_mxnet_model_paths(model)
        reshape = config.mxnet_models[model].get('reshape')
        shape = config.mxnet_models[model].get('shape', (1, 3, 112, 112))
        if custom_shape and reshape == True:
            shape = custom_shape
        output_onnx_dir, output_onnx_model = config.build_model_paths(model, "onnx")
        os.makedirs(output_onnx_dir, exist_ok=True)
        print(f'Converting "{model}" model to ONNX, shape {shape}...')
        convert_insight_model(mxnet_symbol, mxnet_params, output_onnx_model, shape)

