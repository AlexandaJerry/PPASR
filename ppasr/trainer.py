import io
import json
import os
import platform
import shutil
import time
from collections import Counter
from contextlib import nullcontext
from datetime import timedelta

import paddle
from paddle.distributed import fleet
from paddle.io import DataLoader
from tqdm import tqdm
from visualdl import LogWriter

from ppasr import SUPPORT_MODEL
from ppasr.data_utils.collate_fn import collate_fn
from ppasr.data_utils.featurizer.audio_featurizer import AudioFeaturizer
from ppasr.data_utils.featurizer.text_featurizer import TextFeaturizer
from ppasr.data_utils.normalizer import FeatureNormalizer
from ppasr.data_utils.reader import PPASRDataset
from ppasr.data_utils.sampler import SortagradBatchSampler, SortagradDistributedBatchSampler
from ppasr.data_utils.utils import create_manifest_binary
from ppasr.decoders.ctc_greedy_decoder import greedy_decoder_batch
from ppasr.utils.logger import setup_logger
from ppasr.utils.metrics import cer, wer
from ppasr.utils.scheduler import WarmupLR
from ppasr.utils.utils import create_manifest, create_noise, count_manifest, dict_to_object, merge_audio
from ppasr.utils.utils import labels_to_string

logger = setup_logger(__name__)


class PPASRTrainer(object):
    def __init__(self, configs, use_gpu=True):
        """ PPASR集成工具类

        :param configs: 配置字典
        :param use_gpu: 是否使用GPU训练模型
        """
        if use_gpu:
            assert paddle.is_compiled_with_cuda(), 'GPU不可用'
            paddle.device.set_device("gpu")
        else:
            os.environ['CUDA_VISIBLE_DEVICES'] = '-1'
            paddle.device.set_device("cpu")
        self.use_gpu = use_gpu
        self.configs = dict_to_object(configs)
        assert self.configs.use_model in SUPPORT_MODEL, f'没有该模型：{self.configs.use_model}'
        self.model = None
        self.test_loader = None
        self.beam_search_decoder = None

    def __setup_dataloader(self, augment_conf_path=None, is_train=False):
        # 获取训练数据
        if augment_conf_path is not None and os.path.exists(augment_conf_path) and is_train:
            augmentation_config = io.open(augment_conf_path, mode='r', encoding='utf8').read()
        else:
            if augment_conf_path is not None and not os.path.exists(augment_conf_path):
                logger.info('数据增强配置文件{}不存在'.format(augment_conf_path))
            augmentation_config = '{}'
        if not os.path.exists(self.configs.dataset_conf.mean_istd_path):
            raise Exception(f'归一化列表文件 {self.configs.dataset_conf.mean_istd_path} 不存在')
        if is_train:
            self.train_dataset = PPASRDataset(preprocess_configs=self.configs.preprocess_conf,
                                              data_manifest=self.configs.dataset_conf.train_manifest,
                                              vocab_filepath=self.configs.dataset_conf.dataset_vocab,
                                              min_duration=self.configs.dataset_conf.min_duration,
                                              max_duration=self.configs.dataset_conf.max_duration,
                                              augmentation_config=augmentation_config,
                                              manifest_type=self.configs.dataset_conf.get('manifest_type', 'txt'),
                                              train=is_train)
            # 设置支持多卡训练
            if paddle.distributed.get_world_size() > 1 and self.use_gpu:
                self.train_batch_sampler = SortagradDistributedBatchSampler(self.train_dataset,
                                                                            batch_size=self.configs.dataset_conf.batch_size,
                                                                            sortagrad=True,
                                                                            drop_last=True,
                                                                            shuffle=True)
            else:
                self.train_batch_sampler = SortagradBatchSampler(self.train_dataset,
                                                                 batch_size=self.configs.dataset_conf.batch_size,
                                                                 sortagrad=True,
                                                                 drop_last=True,
                                                                 shuffle=True)
            self.train_loader = DataLoader(dataset=self.train_dataset,
                                           collate_fn=collate_fn,
                                           batch_sampler=self.train_batch_sampler,
                                           num_workers=self.configs.dataset_conf.num_workers)
        # 获取测试数据
        self.test_dataset = PPASRDataset(preprocess_configs=self.configs.preprocess_conf,
                                         data_manifest=self.configs.dataset_conf.test_manifest,
                                         vocab_filepath=self.configs.dataset_conf.dataset_vocab,
                                         manifest_type=self.configs.dataset_conf.get('manifest_type', 'txt'),
                                         min_duration=self.configs.dataset_conf.min_duration,
                                         max_duration=self.configs.dataset_conf.max_duration)
        self.test_loader = DataLoader(dataset=self.test_dataset,
                                      batch_size=self.configs.dataset_conf.batch_size,
                                      collate_fn=collate_fn,
                                      num_workers=self.configs.dataset_conf.num_workers)

    def __setup_model(self, input_dim, vocab_size, is_train=False):
        from ppasr.model_utils.conformer.model import ConformerModelOnline, ConformerModelOffline
        from ppasr.model_utils.deepspeech2.model import DeepSpeech2ModelOnline, DeepSpeech2ModelOffline
        # 获取模型
        if self.configs.use_model == 'conformer_online':
            self.model = ConformerModelOnline(configs=self.configs,
                                              input_dim=input_dim,
                                              vocab_size=vocab_size,
                                              **self.configs.model_conf)
        elif self.configs.use_model == 'conformer_offline':
            self.model = ConformerModelOffline(configs=self.configs,
                                               input_dim=input_dim,
                                               vocab_size=vocab_size,
                                               **self.configs.model_conf)
        elif self.configs.use_model == 'deepspeech2_online':
            self.model = DeepSpeech2ModelOnline(configs=self.configs,
                                                input_dim=input_dim,
                                                vocab_size=vocab_size)
        elif self.configs.use_model == 'deepspeech2_offline':
            self.model = DeepSpeech2ModelOffline(configs=self.configs,
                                                 input_dim=input_dim,
                                                 vocab_size=vocab_size)
        else:
            raise Exception('没有该模型：{}'.format(self.configs.use_model))
        # print(self.model)
        if is_train:
            # 设置优化方法
            grad_clip = paddle.nn.ClipGradByGlobalNorm(clip_norm=self.configs.train_conf.grad_clip)
            self.scheduler = WarmupLR(warmup_steps=self.configs.optimizer_conf.warmup_steps,
                                      learning_rate=float(self.configs.optimizer_conf.learning_rate))
            self.optimizer = paddle.optimizer.Adam(parameters=self.model.parameters(),
                                                   learning_rate=self.scheduler,
                                                   weight_decay=float(self.configs.optimizer_conf.weight_decay),
                                                   grad_clip=grad_clip)

    def __load_pretrained(self, pretrained_model):
        # 加载预训练模型
        if pretrained_model is not None:
            if os.path.isdir(pretrained_model):
                pretrained_model = os.path.join(pretrained_model, 'model.pdparams')
            assert os.path.exists(pretrained_model), f"{pretrained_model} 模型不存在！"
            model_dict = self.model.state_dict()
            model_state_dict = paddle.load(pretrained_model)
            # 过滤不存在的参数
            for name, weight in model_dict.items():
                if name in model_state_dict.keys():
                    if list(weight.shape) != list(model_state_dict[name].shape):
                        logger.warning('{} not used, shape {} unmatched with {} in model.'.
                                       format(name, list(model_state_dict[name].shape), list(weight.shape)))
                        model_state_dict.pop(name, None)
                else:
                    logger.warning('Lack weight: {}'.format(name))
            self.model.set_state_dict(model_state_dict)
            logger.info('成功加载预训练模型：{}'.format(pretrained_model))

    def __load_checkpoint(self, save_model_path, resume_model):
        last_epoch = -1
        best_error_rate = 1.0
        last_model_dir = os.path.join(save_model_path,
                                      f'{self.configs.use_model}_{self.configs.preprocess_conf.feature_method}',
                                      'last_model')
        if resume_model is not None or (os.path.exists(os.path.join(last_model_dir, 'model.pdparams'))
                                        and os.path.exists(os.path.join(last_model_dir, 'optimizer.pdopt'))):
            # 自动获取最新保存的模型
            if resume_model is None: resume_model = last_model_dir
            assert os.path.exists(os.path.join(resume_model, 'model.pdparams')), "模型参数文件不存在！"
            assert os.path.exists(os.path.join(resume_model, 'optimizer.pdopt')), "优化方法参数文件不存在！"
            self.model.set_state_dict(paddle.load(os.path.join(resume_model, 'model.pdparams')))
            self.optimizer.set_state_dict(paddle.load(os.path.join(resume_model, 'optimizer.pdopt')))
            with open(os.path.join(resume_model, 'model.state'), 'r', encoding='utf-8') as f:
                json_data = json.load(f)
                last_epoch = json_data['last_epoch'] - 1
                if 'test_cer' in json_data.keys():
                    best_error_rate = abs(json_data['test_cer'])
                if 'test_wer' in json_data.keys():
                    best_error_rate = abs(json_data['test_wer'])
            logger.info('成功恢复模型参数和优化方法参数：{}'.format(resume_model))
        return last_epoch, best_error_rate

    # 保存模型
    def __save_checkpoint(self, save_model_path, epoch_id, error_rate=1.0, test_loss=1e3, best_model=False):
        if best_model:
            model_path = os.path.join(save_model_path,
                                      f'{self.configs.use_model}_{self.configs.preprocess_conf.feature_method}',
                                      'best_model')
        else:
            model_path = os.path.join(save_model_path,
                                      f'{self.configs.use_model}_{self.configs.preprocess_conf.feature_method}',
                                      'epoch_{}'.format(epoch_id))
        os.makedirs(model_path, exist_ok=True)
        try:
            paddle.save(self.optimizer.state_dict(), os.path.join(model_path, 'optimizer.pdopt'))
            paddle.save(self.model.state_dict(), os.path.join(model_path, 'model.pdparams'))
        except Exception as e:
            logger.error(f'保存模型时出现错误，错误信息：{e}')
            return
        with open(os.path.join(model_path, 'model.state'), 'w', encoding='utf-8') as f:
            f.write('{"last_epoch": %d, "test_%s": %f, "test_loss": %f}' % (
                epoch_id, self.configs.metrics_type, error_rate, test_loss))
        if not best_model:
            last_model_path = os.path.join(save_model_path,
                                           f'{self.configs.use_model}_{self.configs.preprocess_conf.feature_method}',
                                           'last_model')
            shutil.rmtree(last_model_path, ignore_errors=True)
            shutil.copytree(model_path, last_model_path)
            # 删除旧的模型
            old_model_path = os.path.join(save_model_path,
                                          f'{self.configs.use_model}_{self.configs.preprocess_conf.feature_method}',
                                          'epoch_{}'.format(epoch_id - 3))
            if os.path.exists(old_model_path):
                shutil.rmtree(old_model_path)
        logger.info('已保存模型：{}'.format(model_path))

    def __decoder_result(self, outs, vocabulary):
        # 集束搜索方法的处理
        if self.configs.decoder == "ctc_beam_search" and self.beam_search_decoder is None:
            if platform.system() != 'Windows':
                try:
                    from ppasr.decoders.beam_search_decoder import BeamSearchDecoder
                    self.beam_search_decoder = BeamSearchDecoder(vocab_list=vocabulary,
                                                                 **self.configs.ctc_beam_search_decoder_conf)
                except ModuleNotFoundError:
                    logger.warning('==================================================================')
                    logger.warning('缺少 paddlespeech-ctcdecoders 库，请根据文档安装。')
                    logger.warning('【注意】已自动切换为ctc_greedy解码器，ctc_greedy解码器准确率相对较低。')
                    logger.warning('==================================================================\n')
                    self.configs.decoder = 'ctc_greedy'
            else:
                logger.warning('==================================================================')
                logger.warning(
                    '【注意】Windows不支持ctc_beam_search，已自动切换为ctc_greedy解码器，ctc_greedy解码器准确率相对较低。')
                logger.warning('==================================================================\n')
                self.configs.decoder = 'ctc_greedy'

        # 执行解码
        outs = [outs[i, :, :] for i, _ in enumerate(range(outs.shape[0]))]
        if self.configs.decoder == 'ctc_greedy':
            result = greedy_decoder_batch(outs, vocabulary)
        else:
            result = self.beam_search_decoder.decode_batch_beam_search_offline(probs_split=outs)
        return result

    def __train_epoch(self, epoch_id, save_model_path, local_rank, writer, nranks):
        train_times = []
        start = time.time()
        sum_batch = len(self.train_loader) * self.configs.train_conf.max_epoch
        for batch_id, batch in enumerate(self.train_loader()):
            inputs, labels, input_lens, label_lens = batch
            num_utts = label_lens.shape[0]
            if num_utts == 0:
                continue
            if nranks > 1 and batch_id % self.configs.train_conf.accum_grad != 0:
                context = self.model.no_sync
            else:
                context = nullcontext
            with context():
                loss_dict = self.model(inputs, input_lens, labels, label_lens)
                loss = loss_dict['loss'] / self.configs.train_conf.accum_grad
                loss.backward()
            # 执行一次梯度计算
            if batch_id % self.configs.train_conf.accum_grad == 0:
                if local_rank == 0 and writer is not None:
                    writer.add_scalar('Train/Loss', loss.numpy(), self.train_step)
                self.optimizer.step()
                self.optimizer.clear_grad()
                self.scheduler.step()
                self.train_step += 1

            # 多卡训练只使用一个进程打印
            train_times.append((time.time() - start) * 1000)
            if batch_id % self.configs.train_conf.log_interval == 0 and local_rank == 0:
                # 计算每秒训练数据量
                train_speed = self.configs.dataset_conf.batch_size / (sum(train_times) / len(train_times) / 1000)
                # 计算剩余时间
                eta_sec = (sum(train_times) / len(train_times)) * (
                        sum_batch - (epoch_id - 1) * len(self.train_loader) - batch_id)
                eta_str = str(timedelta(seconds=int(eta_sec / 1000)))
                logger.info(f'Train epoch: [{epoch_id}/{self.configs.train_conf.max_epoch}], '
                            f'batch: [{batch_id}/{len(self.train_loader)}], loss: {loss.numpy()[0]:.5f},'
                            f' learning rate: {self.scheduler.get_lr():>.8f}, '
                            f'speed: {train_speed:.2f} data/sec, eta: {eta_str}')
                writer.add_scalar('Train/Loss', loss.numpy(), self.train_step)
                train_times = []
            # 固定步数也要保存一次模型
            if batch_id % 10000 == 0 and batch_id != 0 and local_rank == 0:
                self.__save_checkpoint(save_model_path=save_model_path, epoch_id=epoch_id)
            start = time.time()

    def create_data(self,
                    annotation_path='dataset/annotation/',
                    noise_path='dataset/audio/noise',
                    num_samples=1000000,
                    count_threshold=2,
                    is_change_frame_rate=True,
                    max_test_manifest=10000,
                    is_merge_audio=False,
                    save_audio_path='dataset/audio/merge_audio',
                    max_duration=600):
        """
        创建数据列表和词汇表
        :param annotation_path: 标注文件的路径
        :param noise_path: 噪声音频存放的文件夹路径
        :param num_samples: 用于计算均值和标准值得音频数量，当为-1使用全部数据
        :param count_threshold: 字符计数的截断阈值，0为不做限制
        :param is_change_frame_rate: 是否统一改变音频的采样率
        :param max_test_manifest: 生成测试数据列表的最大数量，如果annotation_path包含了test.txt，就全部使用test.txt的数据
        :param is_merge_audio: 是否将多个短音频合并成长音频，以减少音频文件数量，注意自动删除原始音频文件
        :param save_audio_path: 合并音频的保存路径
        :param max_duration: 合并音频的最大长度，单位秒
        """
        if is_merge_audio:
            logger.info('开始合并音频...')
            merge_audio(annotation_path=annotation_path, save_audio_path=save_audio_path, max_duration=max_duration,
                        target_sr=self.configs.preprocess_conf.sample_rate)
            logger.info('合并音频已完成，原始音频文件和标注文件已自动删除，其他原始文件可手动删除！')

        logger.info('开始生成数据列表...')
        create_manifest(annotation_path=annotation_path,
                        train_manifest_path=self.configs.dataset_conf.train_manifest,
                        test_manifest_path=self.configs.dataset_conf.test_manifest,
                        is_change_frame_rate=is_change_frame_rate,
                        max_test_manifest=max_test_manifest,
                        target_sr=self.configs.preprocess_conf.sample_rate)
        logger.info('=' * 70)
        logger.info('开始生成噪声数据列表...')
        create_noise(path=noise_path,
                     noise_manifest_path=self.configs.dataset_conf.noise_manifest_path,
                     is_change_frame_rate=is_change_frame_rate,
                     target_sr=self.configs.preprocess_conf.sample_rate)
        logger.info('=' * 70)

        logger.info('开始生成数据字典...')
        counter = Counter()
        count_manifest(counter, self.configs.dataset_conf.train_manifest)

        count_sorted = sorted(counter.items(), key=lambda x: x[1], reverse=True)
        with open(self.configs.dataset_conf.dataset_vocab, 'w', encoding='utf-8') as fout:
            fout.write('<blank>\t-1\n')
            fout.write('<unk>\t-1\n')
            for char, count in count_sorted:
                if char == ' ': char = '<space>'
                # 跳过指定的字符阈值，超过这大小的字符都忽略
                if count < count_threshold: break
                fout.write('%s\t%d\n' % (char, count))
            fout.write('<eos>\t-1\n')
        logger.info('数据字典生成完成！')

        logger.info('=' * 70)
        normalizer = FeatureNormalizer(mean_istd_filepath=self.configs.dataset_conf.mean_istd_path)
        normalizer.compute_mean_istd(manifest_path=self.configs.dataset_conf.train_manifest,
                                     num_workers=self.configs.dataset_conf.num_workers,
                                     preprocess_configs=self.configs.preprocess_conf,
                                     num_samples=num_samples)
        print('计算的均值和标准值已保存在 %s！' % self.configs.dataset_conf.mean_istd_path)

        if self.configs.dataset_conf.get('manifest_type', 'txt') == 'binary':
            logger.info('=' * 70)
            logger.info('正在生成数据列表的二进制文件...')
            create_manifest_binary(train_manifest_path=self.configs.dataset_conf.train_manifest,
                                   test_manifest_path=self.configs.dataset_conf.test_manifest)
            logger.info('数据列表的二进制文件生成完成！')

    def train(self,
              save_model_path='models/',
              resume_model=None,
              pretrained_model=None,
              augment_conf_path='configs/augmentation.json'):
        """
        训练模型
        :param save_model_path: 模型保存的路径
        :param resume_model: 恢复训练，当为None则不使用预训练模型
        :param pretrained_model: 预训练模型的路径，当为None则不使用预训练模型
        :param augment_conf_path: 数据增强的配置文件，为json格式
        """
        paddle.seed(1000)
        # 训练只能用贪心解码，解码速度快
        self.configs.decoder = 'ctc_greedy'
        # 获取有多少张显卡训练
        nranks = paddle.distributed.get_world_size()
        local_rank = paddle.distributed.get_rank()
        writer = None
        if local_rank == 0:
            # 日志记录器
            writer = LogWriter(logdir='log')

        if nranks > 1 and self.use_gpu:
            # 初始化Fleet环境
            strategy = fleet.DistributedStrategy()
            fleet.init(is_collective=True, strategy=strategy)

        # 获取数据
        self.__setup_dataloader(augment_conf_path=augment_conf_path, is_train=True)
        # 获取模型
        self.__setup_model(input_dim=self.test_dataset.feature_dim,
                           vocab_size=self.test_dataset.vocab_size,
                           is_train=True)

        # 支持多卡训练
        if nranks > 1 and self.use_gpu:
            self.optimizer = fleet.distributed_optimizer(self.optimizer)
            self.model = fleet.distributed_model(self.model)
        logger.info('训练数据：{}'.format(len(self.train_dataset)))

        self.__load_pretrained(pretrained_model=pretrained_model)
        # 加载恢复模型
        last_epoch, best_error_rate = self.__load_checkpoint(save_model_path=save_model_path, resume_model=resume_model)

        test_step, self.train_step = 0, 0
        last_epoch += 1
        self.train_batch_sampler.epoch = last_epoch
        if local_rank == 0:
            writer.add_scalar('Train/lr', self.scheduler.get_lr(), last_epoch)
        # 开始训练
        for epoch_id in range(last_epoch, self.configs.train_conf.max_epoch):
            epoch_id += 1
            start_epoch = time.time()
            # 训练一个epoch
            self.__train_epoch(epoch_id=epoch_id, save_model_path=save_model_path, local_rank=local_rank,
                               writer=writer, nranks=nranks)
            # 多卡训练只使用一个进程执行评估和保存模型
            if local_rank == 0:
                logger.info('=' * 70)
                loss, error_result = self.evaluate(resume_model=None)
                logger.info('Test epoch: {}, time/epoch: {}, loss: {:.5f}, {}: {:.5f}'.format(
                    epoch_id, str(timedelta(seconds=(time.time() - start_epoch))), loss, self.configs.metrics_type,
                    error_result))
                logger.info('=' * 70)
                writer.add_scalar('Test/{}'.format(self.configs.metrics_type), error_result, test_step)
                writer.add_scalar('Test/Loss', loss, test_step)
                test_step += 1
                self.model.train()
                # 记录学习率
                writer.add_scalar('Train/lr', self.scheduler.last_lr, epoch_id)
                # 保存最优模型
                if error_result <= best_error_rate:
                    best_error_rate = error_result
                    self.__save_checkpoint(save_model_path=save_model_path, epoch_id=epoch_id, error_rate=error_result,
                                           test_loss=loss, best_model=True)
                # 保存模型
                self.__save_checkpoint(save_model_path=save_model_path, epoch_id=epoch_id, error_rate=error_result,
                                       test_loss=loss)

    def evaluate(self, resume_model='models/conformer_online_fbank/best_model/'):
        """
        评估模型
        :param resume_model: 所使用的模型
        :return: 评估结果
        """
        if self.test_loader is None:
            self.__setup_dataloader()
        if self.model is None:
            self.__setup_model(input_dim=self.test_dataset.feature_dim,
                               vocab_size=self.test_dataset.vocab_size)
        if resume_model is not None:
            if os.path.isdir(resume_model):
                resume_model = os.path.join(resume_model, 'model.pdparams')
            assert os.path.exists(resume_model), f"{resume_model} 模型不存在！"
            model_state_dict = paddle.load(resume_model)
            self.model.set_state_dict(model_state_dict)
            logger.info(f'成功加载模型：{resume_model}')
        self.model.eval()
        if isinstance(self.model, paddle.DataParallel):
            eval_model = self.model._layers
        else:
            eval_model = self.model

        error_results, losses = [], []
        eos = self.test_dataset.vocab_size - 1
        with paddle.no_grad():
            for batch_id, batch in enumerate(tqdm(self.test_loader())):
                inputs, labels, input_lens, label_lens = batch
                loss_dict = self.model(inputs, input_lens, labels, label_lens)
                losses.append(loss_dict['loss'].numpy()[0] / self.configs.train_conf.accum_grad)
                # 获取模型编码器输出
                outputs = eval_model.get_encoder_out(inputs, input_lens).numpy()
                out_strings = self.__decoder_result(outs=outputs, vocabulary=self.test_dataset.vocab_list)
                labels_str = labels_to_string(labels, self.test_dataset.vocab_list, eos=eos)
                for out_string, label in zip(*(out_strings, labels_str)):
                    # 计算字错率或者词错率
                    if self.configs.metrics_type == 'wer':
                        error_results.append(wer(out_string, label))
                    else:
                        error_results.append(cer(out_string, label))
        loss = float(sum(losses) / len(losses))
        error_result = float(sum(error_results) / len(error_results))
        self.model.train()
        return loss, error_result

    def export(self, save_model_path='models/', resume_model='models/conformer_online_fbank/best_model/'):
        """
        导出预测模型
        :param save_model_path: 模型保存的路径
        :param resume_model: 准备转换的模型路径
        :return:
        """
        # 获取训练数据
        audio_featurizer = AudioFeaturizer(**self.configs.preprocess_conf)
        text_featurizer = TextFeaturizer(self.configs.dataset_conf.dataset_vocab)
        if not os.path.exists(self.configs.dataset_conf.mean_istd_path):
            raise Exception(f'归一化列表文件 {self.configs.dataset_conf.mean_istd_path} 不存在')
        # 获取模型
        self.__setup_model(input_dim=audio_featurizer.feature_dim,
                           vocab_size=text_featurizer.vocab_size)
        # 加载预训练模型
        if os.path.isdir(resume_model):
            resume_model = os.path.join(resume_model, 'model.pdparams')
        assert os.path.exists(resume_model), f"{resume_model} 模型不存在！"
        model_state_dict = paddle.load(resume_model)
        self.model.set_state_dict(model_state_dict)
        logger.info('成功恢复模型参数和优化方法参数：{}'.format(resume_model))
        self.model.eval()
        # 获取静态模型
        infer_model = self.model.export()
        infer_model_dir = os.path.join(save_model_path,
                                       f'{self.configs.use_model}_{self.configs.preprocess_conf.feature_method}',
                                       'infer')
        os.makedirs(infer_model_dir, exist_ok=True)
        infer_model_path = os.path.join(infer_model_dir, 'model')
        paddle.jit.save(infer_model, infer_model_path)
        logger.info("预测模型已保存：{}".format(infer_model_path))
