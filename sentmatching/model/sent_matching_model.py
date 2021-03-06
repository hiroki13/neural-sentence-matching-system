import gzip
import time
import math
import cPickle as pickle
from abc import ABCMeta, abstractmethod

import numpy as np
import theano
import theano.tensor as T

from ..nn.initialization import get_activation_by_name
from ..nn.optimization import create_optimization_updates
from ..nn.basic import LSTM, GRU, CNN, apply_dropout
from ..nn.advanced import RCNN, GRNN, StrCNN
from ..nn.nn_utils import binary_cross_entropy, normalize_2d, normalize_3d, average_without_padding
from ..utils.io_utils import say, PAD


class Model(object):
    __metaclass__ = ABCMeta

    def __init__(self, args, emb_layers):
        self.args = args

        self.emb_layers = emb_layers
        self.layers = []
        self.params = []

        ###################
        # Network options #
        ###################
        self.activation = None
        self.n_d = None
        self.n_e = None
        self.pad_id = None
        self.dropout = None

        ###################
        # Input variables #
        ###################
        self.x = None
        self.y = None
        self.train_inputs = None
        self.pred_inputs = None

        ######################
        # Training objective #
        ######################
        self.loss = None
        self.cost = None

        ##################
        # Testing scores #
        ##################
        self.y_pred = None

    @abstractmethod
    def compile(self):
        raise NotImplementedError

    def set_params(self, layers):
        for l in layers:
            self.params += l.params
        say("num of parameters: {}\n".format(sum(len(x.get_value(borrow=True).ravel()) for x in self.params)))

    @staticmethod
    def set_loss(y, y_scores):
        return T.mean(binary_cross_entropy(y_scores, y))

    @staticmethod
    def set_cost(args, params, loss):
        l2_reg = None
        for p in params:
            if l2_reg is None:
                l2_reg = p.norm(2)
            else:
                l2_reg += p.norm(2)
        return loss + l2_reg * args.l2_reg

    def get_pnorm_stat(self):
        lst_norms = []
        for p in self.params:
            vals = p.get_value(borrow=True)
            l2 = np.linalg.norm(vals)
            lst_norms.append("{:.3f}".format(l2))
        return lst_norms

    def load_pretrained_parameters(self, args):
        with gzip.open(args.load_pretrain) as fin:
            data = pickle.load(fin)
            assert args.hidden_dim == data["d"]
            for l, p in zip(self.layers, data["params"]):
                l.params = p

    def save_model(self, path):
        if not path.endswith(".pkl.gz"):
            path += ".gz" if path.endswith(".pkl") else ".pkl.gz"

        args = self.args
        params = [x.params for x in self.layers]
        with gzip.open(path, "w") as fout:
            pickle.dump(
                {
                    "args": args,
                    "d": args.hidden_dim,
                    "params": params,
                },
                fout,
                protocol=pickle.HIGHEST_PROTOCOL
            )

    @staticmethod
    def evaluate(samples, eval_func):
        crr = 0.
        ttl = 0.
        for sample in samples:
            y_pred = eval_func(*sample[:-1])

            y = sample[-1]
            crr += len([1 for s1, s2 in zip(y, y_pred) if s1 == s2])
            ttl += len(y_pred)

        return crr / ttl

    def get_train_func(self):
        say('\nBuilding functions...\n\n')

        ############################
        # Set the update procedure #
        ############################
        updates, lr, gnorm = create_optimization_updates(
            cost=self.cost,
            params=self.params,
            lr=self.args.learning_rate,
            method=self.args.learning
        )[:3]

        #####################
        # Set the functions #
        #####################
        train_func = theano.function(
            inputs=self.train_inputs,
            outputs=[self.cost, self.loss, gnorm, self.y_pred],
            updates=updates,
#            on_unused_input='ignore'
        )

        say("\tp_norm: {}\n".format(self.get_pnorm_stat()))
        return train_func

    def get_eval_func(self):
        eval_func = theano.function(
            inputs=self.pred_inputs,
            outputs=self.y_pred,
#            on_unused_input='ignore'
        )
        return eval_func

    def train(self, train_func, eval_func, train_samples, dev_samples=None, test_samples=None):
        args = self.args
        unchanged = 0
        best_acc = -1

        max_epoch = args.max_epoch

        batch_indices = range(len(train_samples))

        for epoch in xrange(max_epoch):
            unchanged += 1
            if unchanged > 15:
                break

            np.random.shuffle(batch_indices)

            train_loss = 0.0
            train_cost = 0.0
            crr = 0.
            ttl = 0.

            start_time = time.time()

            for i, index in enumerate(batch_indices):
                sample = train_samples[index]
                cur_cost, cur_loss, grad_norm, y_pred = train_func(*sample)

                if math.isnan(cur_loss):
                    say('\n\nNAN: Index: %d\n' % i)
                    exit()

                train_loss += cur_loss
                train_cost += cur_cost

                y = sample[-1]
                crr += len([1 for s1, s2 in zip(y, y_pred) if s1 == s2])
                ttl += len(y_pred)

                if i % 10 == 0:
                    say("\r{}/{}".format(i, len(batch_indices)))

            # Set the dropout prob for validating
            self.dropout.set_value(0.0)

            if dev_samples is not None:
                dev_acc = self.evaluate(dev_samples, eval_func)
            if test_samples is not None:
                test_acc = self.evaluate(test_samples, eval_func)

            if dev_acc > best_acc:
                unchanged = 0
                best_acc = dev_acc
                if args.save_model:
                    self.save_model(args.save_model)

            # Set the dropout prob for training
            dropout_p = np.float32(args.dropout).astype(theano.config.floatX)
            self.dropout.set_value(dropout_p)

            say("\r\n\n")
            say(("Epoch {}\tcost={:.3f}\tloss={:.3f}" + "\tACC={:.2%},{:.2%}\t|g|={:.3f}\t[{:.3f}m]\n").format(
                epoch,
                train_cost / (i + 1),
                train_loss / (i + 1),
                dev_acc,
                best_acc,
                float(grad_norm),
                (time.time() - start_time) / 60.0
            ))
            say("\tTrain Accuracy: %f (%d/%d)\n" % (crr / ttl, crr, ttl))
            say("\tp_norm: {}\n".format(self.get_pnorm_stat()))
            say("\n")


class BaseModel(Model):

    def compile(self):
        # 1D: n_words, 2D: batch * n_cands
        self.x = T.imatrix()
        self.y = T.fvector()
        self.train_inputs = [self.x, self.y]
        self.pred_inputs = [self.x]

        self.activation = self.args.activation
        self.n_d = self.args.hidden_dim
        self.n_e = self.emb_layers[0].n_d
        self.pad_id = self.emb_layers[0].vocab_map[PAD]
        self.dropout = theano.shared(np.float32(self.args.dropout).astype(theano.config.floatX))

        self._set_layers(args=self.args, n_d=self.n_d, n_e=self.n_e)

        ###########
        # Network #
        ###########
        h_in = self._input_layer(x=self.x)
        h = self._mid_layer(h_prev=h_in, x=self.x, pad_id=self.pad_id)
        y_scores = self._output_layer(h=h)
        self.y_pred = T.le(0.5, y_scores)

        #########################
        # Set an objective func #
        #########################
        self.set_params(layers=self.layers)
        self.loss = self.set_loss(self.y, y_scores)
        self.cost = self.set_cost(args=self.args, params=self.params, loss=self.loss)

    def _set_layers(self, args, n_d, n_e):
        activation = get_activation_by_name(args.activation)

        ##################
        # Set layer type #
        ##################
        if args.layer.lower() == "lstm":
            layer_type = LSTM
        elif args.layer.lower() == "gru":
            layer_type = GRU
        elif args.layer.lower() == "grnn":
            layer_type = GRNN
        elif args.layer.lower() == "cnn":
            layer_type = CNN
        elif args.layer.lower() == "str_cnn":
            layer_type = StrCNN
        else:
            layer_type = RCNN

        ##############
        # Set layers #
        ##############
        for i in range(args.depth):
            if layer_type == CNN or layer_type == StrCNN:
                feature_layer = layer_type(
                    n_in=n_e if i == 0 else n_d,
                    n_out=n_d,
                    activation=activation,
                    order=args.order
                )
            elif layer_type != RCNN:
                feature_layer = layer_type(
                    n_in=n_e if i == 0 else n_d,
                    n_out=n_d,
                    activation=activation
                )
            else:
                feature_layer = layer_type(
                    n_in=n_e if i == 0 else n_d,
                    n_out=n_d,
                    activation=activation,
                    order=args.order,
                    mode=args.mode,
                    has_outgate=args.outgate
                )
            self.layers.append(feature_layer)

    def _input_layer(self, x):
        """
        :param x: 1D: n_words, 2D: n_sents
        :return: 1D: n_words, 2D: n_sents, 3D: n_e
        """
        # 1D: n_words, 2D: n_sents, 3D: n_e
        h = self.emb_layers[0].forward(x)
        return apply_dropout(h, self.dropout)

    def _mid_layer(self, h_prev, x, pad_id):
        """
        :param h_prev: 1D: n_words, 2D: n_sents, 3D: n_e
        :param x: 1D: n_words, 2D: n_sents
        :return: 1D: n_sents, 2D: n_d
        """
        args = self.args

        # 1D: n_words, 2D: n_sents, 3D: n_d
        for i in range(args.depth):
            h = self.layers[i].forward_all(h_prev)
            h_prev = h

        if args.normalize:
            h = normalize_3d(h)

        # 1D: n_sents, 2D: n_d
        if args.average or args.layer == "cnn" or args.layer == "str_cnn":
            h = average_without_padding(h, x, pad_id)
        else:
            h = h[-1]

        h = apply_dropout(h, self.dropout)

        if args.normalize:
            h = normalize_2d(h)

        return h

    @staticmethod
    def _output_layer(h):
        """
        :param h: 1D: n_sents, 2D: n_d
        :return: 1D: n_sents/2
        """
        v = T.arange(h.shape[0] / 2)
        sent_1 = h[v * 2, :]
        sent_2 = h[(v + 1) * 2 - 1, :]
        return T.nnet.sigmoid(T.sum(sent_1 * sent_2, axis=1))


class SemModel(Model):

    def compile(self):
        # 1D: n_words, 2D: batch * 2
        self.x_w = T.imatrix()
        # 1D: batch * 2, 2D: n_words, 3D: n_props
        self.x_s = T.itensor3()
        self.y = T.fvector()
        self.train_inputs = [self.x_w, self.x_s, self.y]
        self.pred_inputs = [self.x_w, self.x_s]

        self.activation = self.args.activation
        self.n_d = self.args.hidden_dim
        self.n_e = self.emb_layers[0].n_d
        self.pad_id = self.emb_layers[0].vocab_map[PAD]
        self.dropout = theano.shared(np.float32(self.args.dropout).astype(theano.config.floatX))

        self._set_layers(args=self.args, n_d=self.n_d, n_e=self.n_e)

        ###########
        # Network #
        ###########
        # 1D: n_words, 2D: n_sents, 3D: n_e
        h_w_in = self._input_layer(x=self.x_w, emb_layer=self.emb_layers[0])
        # 1D: n_sents, 2D: n_words, 3D: n_props, 4D: n_e
        h_s_in = self._input_layer(x=self.x_s, emb_layer=self.emb_layers[1])
        h_s_in = self._average_3d_without_padding(h_s_in, self.x_s, self.emb_layers[1].vocab_map[PAD])
        h_w_in = h_w_in + h_s_in.dimshuffle(1, 0, 2)
        self.u = h_w_in

        # 1D: n_sents, 2D: n_d
        h = self._mid_layer(h_prev=h_w_in, x=self.x_w, pad_id=self.pad_id)
#        h = h + T.mean(T.mean(h_s_in, axis=2), axis=1)

        y_scores = self._output_layer(h=h)
        self.y_pred = T.le(0.5, y_scores)

        #########################
        # Set an objective func #
        #########################
        self.layers.append(self.emb_layers[-1])
        self.set_params(layers=self.layers)
        self.loss = self.set_loss(self.y, y_scores)
        self.cost = self.set_cost(args=self.args, params=self.params, loss=self.loss)

    def _set_layers(self, args, n_d, n_e):
        activation = get_activation_by_name(args.activation)

        ##################
        # Set layer type #
        ##################
        if args.layer.lower() == "lstm":
            layer_type = LSTM
        elif args.layer.lower() == "gru":
            layer_type = GRU
        elif args.layer.lower() == "grnn":
            layer_type = GRNN
        elif args.layer.lower() == "cnn":
            layer_type = CNN
        elif args.layer.lower() == "str_cnn":
            layer_type = StrCNN
        else:
            layer_type = RCNN

        ##############
        # Set layers #
        ##############
        for i in range(args.depth):
            if layer_type == CNN or layer_type == StrCNN:
                feature_layer = layer_type(
                    n_in=n_e if i == 0 else n_d,
                    n_out=n_d,
                    activation=activation,
                    order=args.order
                )
            elif layer_type != RCNN:
                feature_layer = layer_type(
                    n_in=n_e if i == 0 else n_d,
                    n_out=n_d,
                    activation=activation
                )
            else:
                feature_layer = layer_type(
                    n_in=n_e if i == 0 else n_d,
                    n_out=n_d,
                    activation=activation,
                    order=args.order,
                    mode=args.mode,
                    has_outgate=args.outgate
                )
            self.layers.append(feature_layer)

    def _input_layer(self, x, emb_layer):
        """
        :param x: 1D: n_words, 2D: n_sents
        :return: 1D: n_words, 2D: n_sents, 3D: n_e
        """
        # 1D: n_words, 2D: n_sents, 3D: n_e
        h = emb_layer.forward(x)
        return apply_dropout(h, self.dropout)

    def _mid_layer(self, h_prev, x, pad_id):
        """
        :param h_prev: 1D: n_words, 2D: n_sents, 3D: n_e
        :param x: 1D: n_words, 2D: n_sents
        :return: 1D: n_sents, 2D: n_d
        """
        args = self.args

        # 1D: n_words, 2D: n_sents, 3D: n_d
        for i in range(args.depth):
            h = self.layers[i].forward_all(h_prev)
            h_prev = h

        if args.normalize:
            h = normalize_3d(h)

        # 1D: n_sents, 2D: n_d
        if args.average or args.layer == "cnn" or args.layer == "str_cnn":
            h = average_without_padding(h, x, pad_id)
        else:
            h = h[-1]

        h = apply_dropout(h, self.dropout)

        if args.normalize:
            h = normalize_2d(h)

        return h

    @staticmethod
    def _output_layer(h):
        """
        :param h: 1D: n_sents, 2D: n_d
        :return: 1D: n_sents/2
        """
        v = T.arange(h.shape[0] / 2)
        sent_1 = h[v * 2, :]
        sent_2 = h[(v + 1) * 2 - 1, :]
        return T.nnet.sigmoid(T.sum(sent_1 * sent_2, axis=1))

    @staticmethod
    def _get_3d_mask(x, padding_id):
        # 1D: batch * 2, 2D: n_words, 3D: n_props
        mask = T.neq(x, padding_id)
        mask = T.cast(mask, theano.config.floatX)
        return mask

    @staticmethod
    def _average_3d_without_padding(x, ids, padding_id, eps=1e-8):
        """
        :param x: 1D: batch * 2, 2D: n_words, 3D: n_props, 4D: n_d
        :param ids: 1D: batch * 2, 2D: n_words, 3D: n_props
        :return: 1D: batch, 2D: n_words, 3D: n_d
        """
        mask = T.neq(ids, padding_id).dimshuffle((0, 1, 2, 'x'))
        mask = T.cast(mask, theano.config.floatX)
        return T.sum(x * mask, axis=2) / (T.sum(mask, axis=2) + eps)
