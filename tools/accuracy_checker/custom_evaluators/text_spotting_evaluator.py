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
import numpy as np

from accuracy_checker.adapters import create_adapter
from accuracy_checker.config import ConfigError
from accuracy_checker.data_readers import BaseReader
from accuracy_checker.dataset import Dataset
from accuracy_checker.evaluators import BaseEvaluator
from accuracy_checker.launcher import create_launcher
from accuracy_checker.metrics import MetricsExecutor
from accuracy_checker.preprocessor import PreprocessingExecutor
from accuracy_checker.utils import contains_all, extract_image_representations


class TextSpottingEvaluator(BaseEvaluator):
    def __init__(self, dataset, reader, preprocessing, metric_executor, launcher, model):
        self.dataset = dataset
        self.preprocessing_executor = preprocessing
        self.metric_executor = metric_executor
        self.launcher = launcher
        self.model = model
        self.reader = reader
        self._metrics_results = []

    @classmethod
    def from_configs(cls, config):
        dataset_config = config['datasets'][0]
        dataset = Dataset(dataset_config)
        data_reader_config = dataset_config.get('reader', 'opencv_imread')
        data_source = dataset_config['data_source']
        if isinstance(data_reader_config, str):
            reader = BaseReader.provide(data_reader_config, data_source)
        elif isinstance(data_reader_config, dict):
            reader = BaseReader.provide(data_reader_config['type'], data_source, data_reader_config)
        else:
            raise ConfigError('reader should be dict or string')
        preprocessing = PreprocessingExecutor(dataset_config.get('preprocessing', []), dataset.name)
        metrics_executor = MetricsExecutor(dataset_config['metrics'], dataset)
        launcher = create_launcher(config['launchers'][0], delayed_model_loading=True)
        model = SequentialModel(config.get('network_info', {}), launcher)
        return cls(dataset, reader, preprocessing, metrics_executor, launcher, model)

    def process_dataset(self, stored_predictions, progress_reporter, *args, **kwargs):
        self._annotations, self._predictions = ([],
                                                []) if self.metric_executor.need_store_predictions else None, None
        if progress_reporter:
            progress_reporter.reset(self.dataset.size)

        for batch_id, (dataset_indices, batch_annotation) in enumerate(self.dataset):

            batch_identifiers = [annotation.identifier for annotation in batch_annotation]
            batch_input = [self.reader(identifier=identifier) for identifier in batch_identifiers]
            batch_input = self.preprocessing_executor.process(batch_input, batch_annotation)
            batch_input, batch_meta = extract_image_representations(batch_input)
            batch_prediction = self.model.predict(batch_identifiers, batch_input, batch_meta)
            self.metric_executor.update_metrics_on_batch(dataset_indices, batch_annotation,
                                                         batch_prediction)
            if self.metric_executor.need_store_predictions:
                self._annotations.extend(batch_annotation)
                self._predictions.extend(batch_prediction)

            progress_reporter.update(batch_id, len(batch_prediction))

        if progress_reporter:
            progress_reporter.finish()

    def compute_metrics(self, print_results=True, ignore_results_formatting=False):
        if self._metrics_results:
            del self._metrics_results
            self._metrics_results = []

        for result_presenter, evaluated_metric in self.metric_executor.iterate_metrics(
            self._annotations, self._predictions):
            self._metrics_results.append(evaluated_metric)
            if print_results:
                result_presenter.write_result(evaluated_metric, ignore_results_formatting)

        return self._metrics_results

    def print_metrics_results(self, ignore_results_formatting=False):
        if not self._metrics_results:
            self.compute_metrics(True, ignore_results_formatting)
            return
        result_presenters = self.metric_executor.get_metric_presenters()
        for presenter, metric_result in zip(result_presenters, self._metrics_results):
            presenter.write_results(metric_result, ignore_results_formatting)

    def release(self):
        self.model.release()
        self.launcher.release()

    def reset(self):
        self.metric_executor.reset()
        self.model.reset()

    @staticmethod
    def get_processing_info(config):
        module_specific_params = config.get('module_config')
        model_name = config['name']
        dataset_config = module_specific_params['datasets'][0]
        launcher_config = module_specific_params['launchers'][0]
        return (
            model_name, launcher_config['framework'], launcher_config['device'],
            launcher_config.get('tags'),
            dataset_config['name']
        )


class BaseModel:
    def __init__(self, network_info, launcher):
        self.network_info = network_info

    def predict(self, idenitifers, input_data):
        raise NotImplementedError

    def release(self):
        pass


def create_detector(model_config, launcher):
    launcher_model_mapping = {
        'dlsdk': DetectorDLSDKModel
    }
    framework = launcher.config['framework']
    model_class = launcher_model_mapping.get(framework)
    if not model_class:
        raise ValueError('model for framework {} is not supported'.format(framework))
    return model_class(model_config, launcher)


def create_recognizer(model_config, launcher):
    launcher_model_mapping = {
        'dlsdk': RecognizerDLSDKModel
    }
    framework = launcher.config['framework']
    model_class = launcher_model_mapping.get(framework)
    if not model_class:
        raise ValueError('model for framework {} is not supported'.format(framework))
    return model_class(model_config, launcher)


class SequentialModel(BaseModel):
    def __init__(self, network_info, launcher):
        super().__init__(network_info, launcher)
        if not contains_all(network_info, ['detector', 'recognizer_encoder', 'recognizer_decoder']):
            raise ConfigError('network_info should contains detector, encoder and decoder fields')
        self.detector = create_detector(network_info['detector'], launcher)
        self.recognizer_encoder = create_recognizer(network_info['recognizer_encoder'], launcher)
        self.recognizer_decoder = create_recognizer(network_info['recognizer_decoder'], launcher)
        self.recognizer_decoder_inputs = network_info['recognizer_decoder_inputs']
        self.recognizer_decoder_outputs = network_info['recognizer_decoder_outputs']
        self.max_seq_len = int(network_info['max_seq_len'])
        self.adapter = create_adapter(network_info['adapter'])
        self.alphabet = network_info['alphabet']
        self.sos_index = int(network_info['sos_index'])
        self.eos_index = int(network_info['eos_index'])

    def predict(self, idenitifiers, input_data, frame_meta):
        assert len(idenitifiers) == 1

        detector_outputs = self.detector.predict(idenitifiers, input_data)
        text_features = detector_outputs['text_features']

        texts = []
        for feature in text_features:
            feature = self.recognizer_encoder.predict(idenitifiers, {'input': feature})['output']
            feature = np.reshape(feature, (feature.shape[0], feature.shape[1], -1))
            feature = np.transpose(feature, (0, 2, 1))

            hidden_shape = self.recognizer_decoder.network.inputs[
                self.recognizer_decoder_inputs['prev_hidden']].shape
            hidden = np.zeros(hidden_shape)
            prev_symbol_index = np.ones((1,)) * self.sos_index

            text = str()

            for i in range(self.max_seq_len):
                input_to_decoder = {
                    self.recognizer_decoder_inputs['prev_symbol']: prev_symbol_index,
                    self.recognizer_decoder_inputs['prev_hidden']: hidden,
                    self.recognizer_decoder_inputs['encoder_outputs']: feature}
                decoder_outputs = self.recognizer_decoder.predict(idenitifiers, input_to_decoder)
                coder_output = decoder_outputs[
                    self.recognizer_decoder_outputs['symbols_distribution']]
                prev_symbol_index = np.argmax(coder_output, axis=1)
                if prev_symbol_index == self.eos_index:
                    break
                hidden = decoder_outputs[self.recognizer_decoder_outputs['cur_hidden']]
                text += self.alphabet[int(prev_symbol_index)]
            texts.append(text)

        texts = np.array(texts)

        detector_outputs['texts'] = texts
        output = self.adapter.process(detector_outputs, idenitifiers, frame_meta)
        return output

    def reset(self):
        pass

    def release(self):
        self.detector.release()
        self.recognizer_encoder.release()
        self.recognizer_decoder.release()


class DetectorDLSDKModel(BaseModel):
    def __init__(self, network_info, launcher):
        super().__init__(network_info, launcher)
        model_xml = str(network_info['model'])
        model_bin = str(network_info['weights'])
        self.network = launcher.create_ie_network(model_xml, model_bin)
        if not hasattr(launcher, 'plugin'):
            launcher.create_ie_plugin()
        self.exec_network = launcher.plugin.load(self.network)
        self.im_info_name = [x for x in self.network.inputs if len(self.network.inputs[x].shape) == 2][0]
        self.im_data_name = [x for x in self.network.inputs if len(self.network.inputs[x].shape) == 4][0]

    def predict(self, identifiers, input_data):
        input_data = np.array(input_data)
        assert len(input_data.shape) == 4
        assert input_data.shape[0] == 1

        input_data = {self.im_data_name: self.fit_to_input(input_data),
                      self.im_info_name: np.array(
                          [[input_data.shape[1], input_data.shape[2], 1.0]])}

        output = self.exec_network.infer(input_data)

        return output

    def release(self):
        del self.exec_network

    def fit_to_input(self, input_data):
        input_data = np.transpose(input_data, (0, 3, 1, 2))
        input_data = input_data.reshape(self.network.inputs[self.im_data_name].shape)

        return input_data


class RecognizerDLSDKModel(BaseModel):
    def __init__(self, network_info, launcher):
        super().__init__(network_info, launcher)
        model_xml = str(network_info['model'])
        model_bin = str(network_info['weights'])

        self.network = launcher.create_ie_network(model_xml, model_bin)
        if hasattr(launcher, 'plugin'):
            self.exec_network = launcher.plugin.load(self.network)
        else:
            launcher.load_network(self.network)
            self.exec_network = launcher.exec_network

    def predict(self, identifiers, input_data):
        return self.exec_network.infer(input_data)

    def release(self):
        del self.exec_network
