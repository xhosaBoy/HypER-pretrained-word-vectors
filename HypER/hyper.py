# std
import os
import sys
import re
import argparse
import logging
from collections import defaultdict

# 3rd party
import numpy as np
import torch
from torch.optim.lr_scheduler import ExponentialLR

# internal
from load_data import Data
from models import HypE, HypER, DistMult, ConvE, ComplEx
from language_models import language_model_manager as lmm

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s:%(name)s:%(levelname)s:%(message)s')

stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setLevel(logging.INFO)
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

file_handler = logging.FileHandler('hntn_train_validate_and_test_wn18rr_200d_baseline.log')
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)


class Experiment:

    def __init__(self,
                 model_name,
                 learning_rate=0.001,
                 ent_vec_dim=200,
                 rel_vec_dim=200,
                 epochs=100,
                 batch_size=128,
                 decay_rate=0.,
                 cuda=False,
                 input_dropout=0.,
                 hidden_dropout=0.,
                 feature_map_dropout=0.,
                 in_channels=1,
                 out_channels=32,
                 filt_h=3,
                 filt_w=3,
                 label_smoothing=0.):

        self.model_name = model_name
        self.learning_rate = learning_rate
        self.ent_vec_dim = ent_vec_dim
        self.rel_vec_dim = rel_vec_dim
        self.epochs = epochs
        self.batch_size = batch_size
        self.decay_rate = decay_rate
        self.label_smoothing = label_smoothing
        self.cuda = cuda
        self.kwargs = {"input_dropout": input_dropout,
                       "hidden_dropout": hidden_dropout,
                       "feature_map_dropout": feature_map_dropout,
                       "in_channels": in_channels,
                       "out_channels": out_channels,
                       "filt_h": filt_h,
                       "filt_w": filt_w}

    def get_data_idxs(self, data):
        data_idxs = [(self.entity_idxs[data[i][0]],
                      self.relation_idxs[data[i][1]],
                      self.entity_idxs[data[i][2]]) for i in range(len(data))]

        return data_idxs

    @staticmethod
    def get_er_vocab(data):
        er_vocab = defaultdict(list)

        for triple in data:
            er_vocab[(triple[0], triple[1])].append(triple[2])

        return er_vocab

    def get_batch(self, er_vocab, triple_idxs, triple_size, idx):
        batch = triple_idxs[idx:min(idx + self.batch_size, triple_size)]
        targets = np.zeros((len(batch), len(d.entities)))

        for idx, pair in enumerate(batch):
            targets[idx, er_vocab[pair]] = 1.
        targets = torch.FloatTensor(targets)

        if self.cuda:
            targets = targets.cuda()

        return np.array(batch), targets

    def evaluate_costs(self, evaluate_triple_idxs, model):
        costs = []

        er_vocab = self.get_er_vocab(evaluate_triple_idxs)
        er_vocab_pairs = list(er_vocab.keys())
        er_vocab_pairs_size = len(er_vocab_pairs)
        logger.info(f'Number of entity-relational pairs: {er_vocab_pairs_size}')

        for i in range(0, er_vocab_pairs_size, self.batch_size):
            if i % (128 * 100) == 0:
                logger.info(f'Batch: {i + 1} ...')

            triples, targets = self.get_batch(er_vocab, er_vocab_pairs, er_vocab_pairs_size, i)
            e1_idx = torch.tensor(triples[:, 0])
            r_idx = torch.tensor(triples[:, 1])

            if self.cuda:
                e1_idx = e1_idx.cuda()
                r_idx = r_idx.cuda()

            predictions = model.forward(e1_idx, r_idx)

            if self.label_smoothing:
                targets = ((1.0 - self.label_smoothing) * targets) + (1.0 / targets.size(1))

            cost = model.loss(predictions, targets)
            costs.append(cost.item())

        return costs

    def evaluate(self, model, data, epoch, data_type=None):
        data_type_map = {'training': 'TRAINING', 'validation': 'VALIDATION', 'testing': 'TESTING'}
        data_type = data_type_map[data_type] if data_type else 'TRAINING'
        logger.info(f'Starting {data_type} evaluation {epoch}')

        hits = []
        ranks = []

        for i in range(10):
            hits.append([])

        evaluate_triple_idxs = self.get_data_idxs(data)
        evaluation_triple_size = len(evaluate_triple_idxs)
        logger.info(f'Number of evaluation data points: {evaluation_triple_size}')

        logger.info(f'Starting evaluate costs ...')
        costs = self.evaluate_costs(evaluate_triple_idxs, model)
        logger.info(f'Evaluate costs complete!')

        er_vocab = self.get_er_vocab(self.get_data_idxs(d.data)) if data_type == 'TESTING' else \
            self.get_er_vocab(self.get_data_idxs(d.data_train_and_valid))

        for i in range(0, evaluation_triple_size, self.batch_size):
            if i % (128 * 100) == 0:
                logger.info(f'Batch: {i + 1} ...')

            triples, _ = self.get_batch(er_vocab, evaluate_triple_idxs, evaluation_triple_size, i)
            e1_idx = torch.tensor(triples[:, 0])
            r_idx = torch.tensor(triples[:, 1])
            e2_idx = torch.tensor(triples[:, 2])

            if self.cuda:
                e1_idx = e1_idx.cuda()
                r_idx = r_idx.cuda()
                e2_idx = e2_idx.cuda()

            predictions = model.forward(e1_idx, r_idx)

            for j in range(triples.shape[0]):
                filt = er_vocab[(triples[j][0], triples[j][1])]
                target_value = predictions[j, e2_idx[j]].item()
                predictions[j, filt] = 0.0
                predictions[j, e2_idx[j]] = target_value

            sort_values, sort_idxs = torch.sort(predictions, dim=1, descending=True)

            for j in range(triples.shape[0]):
                rank = np.where(sort_idxs[j].cpu() == e2_idx[j].cpu())[0][0]
                ranks.append(rank + 1)

                for hits_level in range(10):
                    if rank <= hits_level:
                        hits[hits_level].append(1.0)
                    else:
                        hits[hits_level].append(0.0)

        logger.info(f'Epoch: {epoch}, Mean evaluation cost_{data_type.lower()}: {np.mean(costs)}')

        logger.info(f'Epoch: {epoch}, Hits @10_{data_type.lower()}: {np.mean(hits[9])}')
        logger.info(f'Epoch: {epoch}, Hits @3_{data_type.lower()}: {np.mean(hits[2])}')
        logger.info(f'Epoch: {epoch}, Hits @1_{data_type.lower()}: {np.mean(hits[0])}')
        logger.info(f'Epoch: {epoch}, Mean rank_{data_type.lower()}: {np.mean(ranks)}')
        logger.info(f'Epoch: {epoch}, Mean reciprocal rank_{data_type.lower()}: {np.mean(1. / np.array(ranks))}')

    def train_and_eval(self, language_model, entity2idx):
        logger.info(f'Training the {model_name} model with {dataset} knowledge graph ...')

        self.entity_idxs = {d.entities[i]: i for i in range(len(d.entities))}
        matrix_entity_len = len(d.entities)
        logger.debug(f'matrix_entity_len: {matrix_entity_len}')
        weights_entity_matrix = np.zeros((matrix_entity_len, self.ent_vec_dim))
        entities_found = 0

        for entity_idx in self.entity_idxs.keys():
            i = self.entity_idxs[entity_idx]
            embedding = []
            words = []
            entity_found = False

            try:
                entity_string = entity2idx[str(entity_idx)]
                entities = [entity_words.split() for entity_words in entity_string.split(',')]

                for entity in entities:
                    logger.debug(f'entity: {entity}')

                    for word in entity:
                        logger.debug(f'word: {word}')
                        word = word.lower()
                        words.append(language_model[word])

                    embedding.append(np.array(words).mean(axis=0))

                weights_entity_matrix[i] = np.array(embedding).mean(axis=0)
                entity_found = True
            except KeyError:
                if not embedding:
                    weights_entity_matrix[i] = np.random.randn(self.ent_vec_dim) * np.sqrt(1 / (self.ent_vec_dim - 1))
                else:
                    weights_entity_matrix[i] = np.array(embedding).mean(axis=0)
            finally:
                if entity_found:
                    entities_found += 1

        logger.info(f'number of entities found: {entities_found}')
        logger.info(f'total number of entities: {len(self.entity_idxs.keys())}')
        logger.info(f'entity pre-trained vector coverage: {(entities_found / len(self.entity_idxs.keys()) * 100):.2f}%')

        self.entity_weights = weights_entity_matrix
        logger.debug(f'weights_entity_matrix size: {weights_entity_matrix.size}')

        self.relation_idxs = {d.relations[i]: i for i in range(len(d.relations))}
        relation2idx = {str(i): re.sub(r'[/]', ' ', d.relations[i]).strip().split() for i in range(len(d.relations))}

        for idx in relation2idx:
            relation2idx[idx] = [item.replace('_', ' ').strip().split() for item in relation2idx[idx]]

        matrix_relation_len = len(d.relations)
        logger.debug(f'matrix_relation_len: {matrix_relation_len}')
        weights_relation_matrix = np.zeros((matrix_relation_len, self.rel_vec_dim))
        relations_found = 0

        for relation_idx in self.relation_idxs.keys():
            i = self.relation_idxs[relation_idx]
            embedding = []
            words = []
            relation_found = False

            try:
                relations = relation2idx[str(i)]

                for relation in relations:
                    logger.debug(f'relation: {relation}')

                    for word in relation:
                        logger.debug(f'word: {word}')
                        word, = re.findall(r'\w+', word.lower())
                        words.append(language_model[word])

                    embedding.append(np.array(words).mean(axis=0))

                weights_relation_matrix[i] = np.array(embedding).mean(axis=0)
                relation_found = True
            except KeyError:
                if not embedding:
                    weights_entity_matrix[i] = np.random.randn(self.rel_vec_dim) * np.sqrt(1 / (self.rel_vec_dim - 1))
                else:
                    weights_relation_matrix[i] = np.array(embedding).mean(axis=0)
            finally:
                if relation_found:
                    relations_found += 1

        logger.info(f'number of relations found: {relations_found}')
        logger.info(f'total number of relations: {len(self.relation_idxs.keys())}')
        logger.info(f'relations pre-trained vector coverage: '
                    f'{(relations_found / len(self.relation_idxs.keys()) * 100):.2f}%')

        self.relation_weights = weights_relation_matrix
        logger.debug(f'weights_relation_matrix size: {weights_relation_matrix.size}')

        train_triple_idxs = self.get_data_idxs(d.train_data)
        train_triple_size = len(train_triple_idxs)
        logger.info(f'Number of training data points: {train_triple_size}')

        if model_name.lower() == "hype":
            model = HypE(d, self.ent_vec_dim, self.rel_vec_dim, **self.kwargs)
        elif model_name.lower() == "hyper":
            model = HypER(d,
                          self.ent_vec_dim,
                          self.rel_vec_dim,
                          weights_entity_matrix,
                          weights_relation_matrix,
                          **self.kwargs)
        elif model_name.lower() == "distmult":
            model = DistMult(d, self.ent_vec_dim, self.rel_vec_dim, **self.kwargs)
        elif model_name.lower() == "conve":
            model = ConvE(d, self.ent_vec_dim, self.rel_vec_dim, **self.kwargs)
        elif model_name.lower() == "complex":
            model = ComplEx(d, self.ent_vec_dim, self.rel_vec_dim, **self.kwargs)
        logger.debug('model parameters: {}'.format({name: value.numel() for name, value in model.named_parameters()}))

        if self.cuda:
            model.cuda()

        model.init()
        opt = torch.optim.Adam(model.parameters(), lr=self.learning_rate)

        if self.decay_rate:
            scheduler = ExponentialLR(opt, self.decay_rate)

        er_vocab = self.get_er_vocab(train_triple_idxs)
        er_vocab_pairs = list(er_vocab.keys())
        er_vocab_pairs_size = len(er_vocab_pairs)
        logger.info(f'Number of entity-relational pairs: {er_vocab_pairs_size}')

        logger.info('Starting Training ...')

        for epoch in range(1, self.epochs + 1):
            logger.info(f'Epoch: {epoch}')

            model.train()
            costs = []
            np.random.shuffle(er_vocab_pairs)

            for j in range(0, er_vocab_pairs_size, self.batch_size):
                if j % (128 * 100) == 0:
                    logger.info(f'Batch: {j + 1} ...')

                triples, targets = self.get_batch(er_vocab, er_vocab_pairs, er_vocab_pairs_size, j)
                opt.zero_grad()
                e1_idx = torch.tensor(triples[:, 0])
                r_idx = torch.tensor(triples[:, 1])

                if self.cuda:
                    e1_idx = e1_idx.cuda()
                    r_idx = r_idx.cuda()

                predictions = model.forward(e1_idx, r_idx)

                if self.label_smoothing:
                    targets = ((1.0 - self.label_smoothing) * targets) + (1.0 / targets.size(1))

                cost = model.loss(predictions, targets)
                cost.backward()
                opt.step()

                costs.append(cost.item())

            if self.decay_rate:
                scheduler.step()

            logger.info(f'Mean training cost: {np.mean(costs)}')

            if epoch % 10 == 0:
                model.eval()
                with torch.no_grad():
                    train_data = np.array(d.train_data)
                    train_data_map = {'WN18': 10000, 'FB15k': 100000, 'WN18RR': 6068, 'FB15k-237': 35070}
                    train_data_sample_size = train_data_map[dataset]
                    train_data = train_data[np.random.choice(train_data.shape[0],
                                                             train_data_sample_size,
                                                             replace=False), :]

                    ogger.info(f'Starting Evaluation: Training ...')
                    self.evaluate(model, train_data, epoch, 'training')
                    logger.info(f'Evaluation: Training complete!')
                    logger.info(f'Starting Evaluation: Validation ...')
                    self.evaluate(model, d.valid_data, epoch, 'validation')
                    logger.info(f'Evaluation: Validation complete!')
                    logger.info(f'Starting Evaluation: Test ...')
                    self.evaluate(model, d.test_data, epoch, 'testing')
                    logger.info(f'Evaluation: Test complete!')

                    logger.info('Checkpointing model ...')
                    torch.save(model.state_dict(), 'HypER.mc')
                    logger.info('Model checkpoint complete!')

            logger.info('Saving final model ...')
            torch.save(model.state_dict(), 'HypER.pt')
            logger.info('Saving final model complete!')


if __name__ == '__main__':
    logger.info('START!')

    parser = argparse.ArgumentParser()
    parser.add_argument('--algorithm',
                        type=str,
                        default="HypER",
                        nargs="?",
                        help='Which algorithm to use: HypER, ConvE, DistMult, or ComplEx')
    parser.add_argument('--dataset',
                        type=str,
                        default="WN18RR",
                        nargs="?",
                        help='Which dataset to use: FB15k, FB15k-237, WN18 or WN18RR')
    parser.add_argument('--languagemodel',
                        type=str,
                        default="Glove",
                        nargs="?",
                        help='Which language model to use: Fasttext or Glove')
    parser.add_argument('--languagemodelversion',
                        type=str,
                        default="6B.200",
                        nargs="?",
                        help='Which Glove version to use: 6B.200 or twitter.27B.200')

    args = parser.parse_args()
    model_name = args.algorithm
    dataset = args.dataset
    language_model_name = args.languagemodel
    language_model_version = args.languagemodelversion

    language_model_dimension_map = {'Glove': 200, 'Fasttext': 300}
    language_model_dimension = language_model_dimension_map[language_model_name]

    logger.info(f'Logging {dataset} dataset ...')

    data_dir = os.path.join('data', dataset)
    logger.debug(f'data_dir: {data_dir}')
    d = Data(data_dir=data_dir, reverse=True)

    logger.info(f'Logging {dataset} dataset complete!')

    torch.backends.cudnn.deterministic = True
    seed = 42
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available:
        torch.cuda.manual_seed_all(seed)

    experiment = Experiment(model_name,
                            epochs=500,
                            batch_size=128,
                            learning_rate=0.001,
                            decay_rate=0.99,
                            ent_vec_dim=language_model_dimension,
                            rel_vec_dim=language_model_dimension,
                            cuda=True,
                            input_dropout=0.2,
                            hidden_dropout=0.3,
                            feature_map_dropout=0.2,
                            in_channels=1,
                            out_channels=32,
                            filt_h=1,
                            filt_w=9,
                            label_smoothing=0.1)

    knowledge_graph_map = {'WN18': 'WN18', 'WN18RR': 'WN18', 'FB15k': 'FB15k', 'FB15k-237': 'FB15k'}
    knowledge_graph = knowledge_graph_map[dataset]
    language_model, entity2idx = lmm.load_language_model(language_model_name, language_model_version, knowledge_graph)
    experiment.train_and_eval(language_model, entity2idx)

    logger.info('DONE!')
