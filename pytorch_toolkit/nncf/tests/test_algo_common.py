"""
 Copyright (c) 2019 Intel Corporation
 Licensed under the Apache License, Version 2.0 (the "License");
 you may not use this file except in compliance with the License.
 You may obtain a copy of the License at
      http://www.apache.org/licenses/LICENSE-2.0
 Unless required by applicable law or agreed to in writing, software
 distributed under the License is distributed on an "AS IS" BASIS,
 WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 See the License for the specific language governing permissions and
 limitations under the License.
"""

import itertools
import os
from functools import partial

import pytest
from torch import nn
from torch.nn import DataParallel

from nncf.helpers import load_state
from nncf.algo_selector import create_compression_algorithm
from nncf.dynamic_graph import reset_context
from nncf.utils import print_statistics
from tests.quantization.test_quant_algo import get_basic_quantization_config, get_basic_asym_quantization_config
from tests.sparsity.magnitude.test_helpers import get_basic_magnitude_sparsity_config
from tests.sparsity.rb.test_algo import get_basic_sparsity_config
from tests.test_helpers import BasicConvTestModel, get_empty_config


class BasicLinearTestModel(nn.Module):
    def __init__(self, size=4):
        super().__init__()
        self.fc = nn.Linear(size, size)

    def forward(self, x):
        return self.fc(x)


def get_const_sparsity_config():
    config = get_empty_config()
    config['compression'] = {'algorithm': 'const_sparsity'}
    return config


def create_test_compression_algo(config, model):
    reset_context('orig')
    reset_context('quantized_graphs')
    compression_algo = create_compression_algorithm(model, config)
    return compression_algo


@pytest.mark.parametrize('config_provider', (get_basic_quantization_config, get_basic_asym_quantization_config,
                                             get_basic_sparsity_config,
                                             get_basic_magnitude_sparsity_config, get_const_sparsity_config),
                         ids=('SymQuantization', 'AsymQuantization', 'Sparsity', 'MagnitudeSparsity', 'ConstSparsity'))
@pytest.mark.parametrize('model_provider', (BasicConvTestModel, BasicLinearTestModel),
                         ids=('Conv2d', 'Linear'))
class TestCompressionAlgos:
    def test_can_export_compressed_model(self, tmp_path, config_provider, model_provider):
        test_path = str(tmp_path.joinpath('test.onnx'))
        model = model_provider()
        config = config_provider()
        compression_algo = create_test_compression_algo(config, model)

        compression_algo.export_model(test_path)
        assert os.path.exists(test_path)

    def test_can_print_stats(self, config_provider, model_provider):
        model = model_provider()
        config = config_provider()

        compression_algo = create_test_compression_algo(config, model)

        print_statistics(compression_algo.statistics())


QUANTIZATION = 'quantization'
SPARSITY_TYPES = ['magnitude', 'rb', 'const']
SPARSITY_ALGOS = ['_'.join([type, 'sparsity']) for type in SPARSITY_TYPES]  # 3S

LOAD_ALGOS = [algo_list for algo_list in itertools.product([QUANTIZATION], SPARSITY_ALGOS)]  # Q + 3S
LOAD_ALGOS += itertools.product(SPARSITY_ALGOS, [QUANTIZATION])  # 3S + Q

SAVE_ALGOS = [[algo] for algo in SPARSITY_ALGOS]  # 3S
SAVE_ALGOS += [[QUANTIZATION]]  # Q
SAVE_ALGOS += LOAD_ALGOS  # Q , 3S, 3S + Q, Q+3S

ALGOS = list(itertools.product(SAVE_ALGOS, LOAD_ALGOS))


@pytest.fixture(scope='module', params=ALGOS,
                ids=['__'.join(['save:' + '_'.join(a[0]),
                                'load:' + '_'.join(a[1])]) for a in ALGOS]
                )
def _algos(request):
    pair_algos = request.param
    save_algos = pair_algos[0]
    load_algos = pair_algos[1]
    resume_ok = False
    # resume expects the same list of algorithms
    if save_algos == load_algos:
        resume_ok = True

    if len(save_algos) == len(load_algos):
        for s, v in zip(save_algos, load_algos):
            # resume works fine for magnitude <-> const combo, because they have similar parameters
            if s != v and ('magnitude' in s and 'const' in v or 'const' in s and 'magnitude' in v):
                resume_ok = True
    return {
        'save_algos': save_algos,
        'load_algos': load_algos,
        'is_resume_ok': resume_ok
    }


MODEL_WRAPPER = ["CPU", "GPU"]
WRAPPERS = list(itertools.product(MODEL_WRAPPER, MODEL_WRAPPER))


@pytest.fixture(scope='function', params=WRAPPERS,
                ids=['_'.join(['from:' + w[0], 'to:' + w[1]]) for w in WRAPPERS])
def _model_wrapper(request):
    modes = request.param

    def wrap_model(mode, model):
        if mode == "GPU":
            model = DataParallel(model, [0])
        return model

    return {
        'save_model': partial(wrap_model, modes[0]),
        'resume_model': partial(wrap_model, modes[1]),
    }


@pytest.mark.parametrize('is_resume', (True, False), ids=['resume', 'load_weights'])
def test_load_state_interoperability(_algos, _model_wrapper, is_resume):
    config_save = get_empty_config()
    config_save['compression'] = [{'algorithm': algo, 'params': {}} for algo in _algos['save_algos']]
    algo_save = create_test_compression_algo(config_save, BasicConvTestModel())
    model_save = _model_wrapper['save_model'](algo_save.model)
    saved_model_state = model_save.state_dict()
    ref_num_loaded = len(saved_model_state)

    config_resume = get_empty_config()
    config_resume['compression'] = [{'algorithm': algo, 'params': {}} for algo in _algos['load_algos']]
    algo_resume = create_test_compression_algo(config_resume, BasicConvTestModel())
    model_resume = _model_wrapper['resume_model'](algo_resume.model)

    if not is_resume or (is_resume and _algos['is_resume_ok']):
        act_num_loaded = load_state(model_resume, saved_model_state, is_resume)

        if ('magnitude_sparsity' in _algos['load_algos'] or 'const_sparsity' in _algos['load_algos']) \
            and 'rb_sparsity' in _algos['save_algos']:
            # no need to load _mask and _uniform
            ref_num_loaded -= 2
        assert act_num_loaded == ref_num_loaded
    else:
        with pytest.raises(RuntimeError):
            load_state(model_resume, saved_model_state, is_resume)


LIST_ALGOS = [None, QUANTIZATION]
LIST_ALGOS += SPARSITY_ALGOS  # 3S


@pytest.mark.parametrize('is_resume', (True, False), ids=['resume', 'load_weights'])
@pytest.mark.parametrize('algo', tuple(LIST_ALGOS))
def test_ordinary_load(algo, _model_wrapper, is_resume):
    config = get_empty_config()
    if algo:
        config['compression'] = {'algorithm': algo, 'params': {}}

    algo_save = create_test_compression_algo(config, BasicConvTestModel())
    model_save = _model_wrapper['save_model'](algo_save.model)

    algo_resume = create_test_compression_algo(config, BasicConvTestModel())
    model_resume = _model_wrapper['resume_model'](algo_resume.model)

    num_loaded = load_state(model_resume, model_save.state_dict(), is_resume)

    assert num_loaded == len(model_save.state_dict())
