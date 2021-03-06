""" Code for the MAML algorithm and network definitions. """
import numpy as np
#import special_grads
import tensorflow as tf

from tensorflow.python.platform import flags
from utils import mse, xent, conv_block, normalize

FLAGS = flags.FLAGS

class MAML:
    def __init__(self, dim_input=1, dim_output=1, test_num_updates=5):
        """ must call construct_model() after initializing MAML! """
        self.dim_input = dim_input
        self.dim_output = dim_output
        self.update_lr = FLAGS.update_lr
        self.meta_lr = tf.placeholder_with_default(FLAGS.meta_lr, ())
        self.classification = False
        self.test_num_updates = test_num_updates

        self.temp = tf.placeholder(tf.float32)

        if FLAGS.datasource == 'sinusoid':
            self.dim_hidden = [40, 40]
            self.loss_func = mse
            self.forward = self.forward_fc
            self.construct_weights = self.construct_fc_weights
        elif FLAGS.datasource == 'omniglot' or FLAGS.datasource == 'miniimagenet':
            self.loss_func = xent
            self.classification = True
            if FLAGS.conv:
                self.dim_hidden = FLAGS.num_filters
                self.forward = self.forward_conv
                self.construct_weights = self.construct_conv_weights
            else:
                self.dim_hidden = [256, 128, 64, 64]
                self.forward=self.forward_fc
                self.construct_weights = self.construct_fc_weights
            if FLAGS.datasource == 'miniimagenet':
                self.channels = 3
            else:
                self.channels = 1
            self.img_size = int(np.sqrt(self.dim_input/self.channels))
        else:
            raise ValueError('Unrecognized data source.')

    def construct_model(self, input_tensors=None, prefix='metatrain_'):
        # a: training data for inner gradient, b: test data for meta gradient
        if input_tensors is None:
            self.inputa = tf.placeholder(tf.float32)
            self.inputb = tf.placeholder(tf.float32)
            self.labela = tf.placeholder(tf.float32)
            self.labelb = tf.placeholder(tf.float32)
        else:
            self.inputa = input_tensors['inputa']
            self.inputb = input_tensors['inputb']
            self.labela = input_tensors['labela']
            self.labelb = input_tensors['labelb']

        with tf.variable_scope('model', reuse=None) as training_scope:
            if 'weights_all' in dir(self):
                training_scope.reuse_variables()
                weights_all = self.weights_all
            else:
                # Define the weights
                self.weights_all = weights_all = self.construct_weights()

            # outputbs[i] and lossesb[i] is the output and loss after i+1 gradient updates
            lossesa, outputas, lossesb, outputbs = [], [], [], []
            accuraciesa, accuraciesb = [], []
            num_updates = max(self.test_num_updates, FLAGS.num_updates)
            outputbs = [[]]*num_updates
            lossesb = [[]]*num_updates
            accuraciesb = [[]]*num_updates

            def task_metalearn(inp, reuse=True):
                """ Perform gradient descent for one task in the meta-batch. """
                inputa, inputb, labela, labelb = inp
                task_outputbs, task_lossesb = [], []
                task_gateb = []

                if self.classification:
                    task_accuraciesb = []
                
                # gate return
                weights = {}
                for k,v in weights_all.items():
                    if k.endswith('_0'): # gating variables
                        weights[k] = v
                task_gateb = self.forward(inputb, weights, index=0, reuse=reuse)

                for e in range(1, FLAGS.num_mixtures+1):
                    # initialize weights for expert_e here
                    weights = {}
                    for k,v in weights_all.items():
                        if k.endswith('_'+str(e)):
                            weights[k] = v

                    task_outputa = self.forward(inputa, weights, index=e, reuse=reuse)  # only reuse on the first iter
                    task_lossa = self.loss_func(task_outputa, labela)

                    grads = tf.gradients(task_lossa, list(weights.values()))
                    if FLAGS.stop_grad:
                        grads = [tf.stop_gradient(grad) for grad in grads]
                    gradients = dict(zip(weights.keys(), grads))
                    fast_weights = dict(zip(weights.keys(), [weights[key] - self.update_lr*gradients[key] for key in weights.keys()]))
                    output = self.forward(inputb, fast_weights, index=e, reuse=True)
                    #task_outputbs.append(output)
                    #task_lossesb.append(self.loss_func(output, labelb))

                    for j in range(num_updates - 1):
                        loss = self.loss_func(self.forward(inputa, fast_weights, index=e, reuse=True), labela)

                        grads = tf.gradients(loss, list(fast_weights.values()))
                        if FLAGS.stop_grad:
                            grads = [tf.stop_gradient(grad) for grad in grads]
                        gradients = dict(zip(fast_weights.keys(), grads))
                        fast_weights = dict(zip(fast_weights.keys(), [fast_weights[key] - self.update_lr*gradients[key] for key in fast_weights.keys()]))
                        output = self.forward(inputb, fast_weights, index=e, reuse=True)
                        #task_outputbs.append(output)
                        #task_lossesb.append(self.loss_func(output, labelb))
                    
                    # for each expert, append outputb and lossb
                    task_outputbs.append(output)
                    task_lossesb.append(self.loss_func(output, labelb))

                    task_output = [task_outputa, task_outputbs, task_lossa, task_lossesb, task_gateb]

                if self.classification:
                    task_accuracya = tf.contrib.metrics.accuracy(tf.argmax(tf.nn.softmax(task_outputa), 1), tf.argmax(labela, 1))
                    for j in range(FLAGS.num_mixtures):
                        task_accuraciesb.append(tf.contrib.metrics.accuracy(tf.argmax(tf.nn.softmax(task_outputbs[j]), 1), tf.argmax(labelb, 1)))
                    task_output.extend([task_accuracya, task_accuraciesb])

                return task_output

            if FLAGS.norm is not 'None':
                # to initialize the batch norm vars, might want to combine this, and not run idx 0 twice.
                unused = task_metalearn((self.inputa[0], self.inputb[0], self.labela[0], self.labelb[0]), False)

            out_dtype = [tf.float32, [tf.float32]*FLAGS.num_mixtures, tf.float32, [tf.float32]*FLAGS.num_mixtures, tf.float32]
            if self.classification:
                out_dtype.extend([tf.float32, [tf.float32]*FLAGS.num_mixtures])
            result = tf.map_fn(task_metalearn, elems=(self.inputa, self.inputb, self.labela, self.labelb), dtype=out_dtype, parallel_iterations=FLAGS.meta_batch_size)
            if self.classification:
                outputas, outputbs, lossesa, lossesb, task_gateb, accuraciesa, accuraciesb = result
            else:
                outputas, outputbs, lossesa, lossesb, task_gateb = result

        ## gating here: TODO
        # outputbs dims: n_moe list of mbs*n_tr*n_cls
        # task_gateb dims: mbs*n_tr*n_moe
        print 'len(outputbs):', len(outputbs)
        print 'tf.shape(outputbs[0]):', outputbs[0]
        print 'tf.shape(task_gateb):', task_gateb
        
        expert_distribution = tf.stack(outputbs, 0)           # dim: n_moe*mbs*n_tr*n_cls
        ## first gating then softmax
        # expert_distribution = tf.nn.softmax(expert_distribution) # softmax on cls dimention
        expert_distribution = tf.transpose(expert_distribution, perm=[1, 2, 3, 0])      # mbs*n_tr*n_cls*n_moe

        if FLAGS.uniform_expert:  # uniform weights on all experts
            gating_distribution = tf.zeros_like(expert_distribution)
            gates = tf.reduce_mean(gating_distribution, 2) # dim: mbs*n_tr*n_cls*n_moe -->mbs*n_tr*n_moe
        elif FLAGS.onehot_expert: # onehot weights 1 for expert0
            mask1 = 100*tf.ones_like(expert_distribution[:,:,:,0])
            mask2 = tf.ones_like(expert_distribution[:,:,:,1:])
            gating_distribution = tf.concat([tf.expand_dims(mask1,-1), mask2], -1)
            gates = tf.reduce_mean(gating_distribution, 2) # dim: mbs*n_tr*n_cls*n_moe -->mbs*n_tr*n_moe
        else:
            # gate dimention: ambs*n_tr*n_moe
	    gates = task_gateb
            # expand_dims: mbs*n_tr*1*n_moe, tile: mbs*n_tr*n_cls*n_moe
            gates = gates/(self.temp + 1.0)  # use temprature before softmax
            gating_distribution = tf.tile(tf.expand_dims(gates, 2), [1,1,self.dim_output,1])

        expert_distribution = tf.reshape(
            expert_distribution,
            [-1, FLAGS.num_mixtures])  # (mbs*n_tr*n_cls) x n_moe
        gating_distribution = tf.nn.softmax(tf.reshape(
            gating_distribution,
            [-1, FLAGS.num_mixtures])) # (mbs*n_tr*n_cls) x n_moe

        moe_output = tf.reduce_sum(
            gating_distribution * expert_distribution, 1)
        moe_output = tf.reshape(moe_output, [-1, self.dim_output])

	# sample gating for output to check
        self.gating_distribution = tf.nn.softmax(gates)


        ## Performance & Optimization
        moe_label = tf.reshape(self.labelb, [-1, self.dim_output])
        self.moe_loss = xent(moe_output, moe_label)
        self.moe_acc  = tf.contrib.metrics.accuracy(tf.argmax(tf.nn.softmax(moe_output), 1), tf.argmax(moe_label, 1))
        # use cross_entropy loss instead of softmax_with_cross_entropy
        # self.moe_loss = cross_entropy_loss(moe_output, moe_label)
        # self.moe_acc  = tf.contrib.metrics.accuracy(tf.argmax(moe_output, 1), tf.argmax(moe_label, 1))

        if 'train' in prefix:
            # self.total_loss1 = total_loss1 = tf.reduce_sum(lossesa) / tf.to_float(FLAGS.meta_batch_size)
            self.total_loss1 = total_loss1 = tf.reduce_sum(self.moe_loss) / tf.to_float(FLAGS.meta_batch_size)
            self.total_losses2 = total_losses2 = [tf.reduce_sum(lossesb[j]) / tf.to_float(FLAGS.meta_batch_size) for j in range(FLAGS.num_mixtures)]
            # after the map_fn
            if self.classification:
                # self.total_accuracy1 = total_accuracy1 = tf.reduce_sum(accuraciesa) / tf.to_float(FLAGS.meta_batch_size)
                self.total_accuracy1 = total_accuracy1 = self.moe_acc
                self.total_accuracies2 = total_accuracies2 = [tf.reduce_sum(accuraciesb[j]) / tf.to_float(FLAGS.meta_batch_size) for j in range(FLAGS.num_mixtures)]

            # self.pretrain_op = tf.train.AdamOptimizer(self.meta_lr).minimize(total_loss1)

            if FLAGS.metatrain_iterations > 0:
                optimizer = tf.train.AdamOptimizer(self.meta_lr)
                if FLAGS.uniform_loss:
                    self.gvs = gvs = optimizer.compute_gradients(tf.reduce_sum(self.total_losses2))
                elif FLAGS.total_loss:
                    loss2 = tf.reduce_sum(self.total_losses2)
                    self.gvs = gvs = optimizer.compute_gradients(total_loss1 + 0.2/4*loss2)
                else:
                    # self.gvs = gvs = optimizer.compute_gradients(self.total_losses2[FLAGS.num_mixtures-1])
                    self.gvs = gvs = optimizer.compute_gradients(total_loss1)
                if FLAGS.datasource == 'miniimagenet':
                    gvs_new = []
                    for grad, var in gvs:
                        if not grad==None:
                            gvs_new.append((tf.clip_by_value(grad, -10, 10), var))
                        else:
                            # gvs_new.append((tf.zeros_like(var), var))
                            print var
                    gvs = gvs_new
                    #gvs = [(tf.clip_by_value(grad, -10, 10), var) for grad, var in gvs]
                self.metatrain_op = optimizer.apply_gradients(gvs)
        else:
            #self.metaval_total_loss1 = total_loss1 = tf.reduce_sum(lossesa) / tf.to_float(FLAGS.meta_batch_size)
            self.metaval_total_loss1 = total_loss1 = tf.reduce_sum(self.moe_loss) / tf.to_float(FLAGS.meta_batch_size)
            self.metaval_total_losses2 = total_losses2 = [tf.reduce_sum(lossesb[j]) / tf.to_float(FLAGS.meta_batch_size) for j in range(FLAGS.num_mixtures)]
            if self.classification:
                # self.metaval_total_accuracy1 = total_accuracy1 = tf.reduce_sum(accuraciesa) / tf.to_float(FLAGS.meta_batch_size)
                self.metaval_total_accuracy1 = total_accuracy1 = self.moe_acc
                self.metaval_total_accuracies2 = total_accuracies2 =[tf.reduce_sum(accuraciesb[j]) / tf.to_float(FLAGS.meta_batch_size) for j in range(FLAGS.num_mixtures)]

        ## Summaries
        tf.summary.scalar(prefix+'moe loss', total_loss1)
        if self.classification:
            tf.summary.scalar(prefix+'moe accuracy', total_accuracy1)

        for j in range(FLAGS.num_mixtures):
            tf.summary.scalar(prefix+'expert loss, step ' + str(j+1), total_losses2[j])
            if self.classification:
                tf.summary.scalar(prefix+'expert accuracy, step ' + str(j+1), total_accuracies2[j])

    ### Network construction functions (fc networks and conv networks)
    def construct_fc_weights(self):
        weights = {}
        weights['w1'] = tf.Variable(tf.truncated_normal([self.dim_input, self.dim_hidden[0]], stddev=0.01))
        weights['b1'] = tf.Variable(tf.zeros([self.dim_hidden[0]]))
        for i in range(1,len(self.dim_hidden)):
            weights['w'+str(i+1)] = tf.Variable(tf.truncated_normal([self.dim_hidden[i-1], self.dim_hidden[i]], stddev=0.01))
            weights['b'+str(i+1)] = tf.Variable(tf.zeros([self.dim_hidden[i]]))
        weights['w'+str(len(self.dim_hidden)+1)] = tf.Variable(tf.truncated_normal([self.dim_hidden[-1], self.dim_output], stddev=0.01))
        weights['b'+str(len(self.dim_hidden)+1)] = tf.Variable(tf.zeros([self.dim_output]))
        return weights

    def forward_fc(self, inp, weights, reuse=False):
        hidden = normalize(tf.matmul(inp, weights['w1']) + weights['b1'], activation=tf.nn.relu, reuse=reuse, scope='0')
        for i in range(1,len(self.dim_hidden)):
            hidden = normalize(tf.matmul(hidden, weights['w'+str(i+1)]) + weights['b'+str(i+1)], activation=tf.nn.relu, reuse=reuse, scope=str(i+1))
        return tf.matmul(hidden, weights['w'+str(len(self.dim_hidden)+1)]) + weights['b'+str(len(self.dim_hidden)+1)]

    def construct_conv_weights(self):
        weights = {}

        dtype = tf.float32
        conv_initializer =  tf.contrib.layers.xavier_initializer_conv2d(dtype=dtype)
        fc_initializer =  tf.contrib.layers.xavier_initializer(dtype=dtype)
        k = 3
        
        # 0 for gating, 1~N for experts
        for e in xrange(FLAGS.num_mixtures+1):
            weights['conv1_'+str(e)] = tf.get_variable('conv1_'+str(e), [k, k, self.channels, self.dim_hidden], initializer=conv_initializer, dtype=dtype)
            weights['b1_'+str(e)] = tf.Variable(tf.zeros([self.dim_hidden]), name='b1_'+str(e))
            weights['conv2_'+str(e)] = tf.get_variable('conv2_'+str(e), [k, k, self.dim_hidden, self.dim_hidden], initializer=conv_initializer, dtype=dtype)
            weights['b2_'+str(e)] = tf.Variable(tf.zeros([self.dim_hidden]), name='b2_'+str(e))
            weights['conv3_'+str(e)] = tf.get_variable('conv3_'+str(e), [k, k, self.dim_hidden, self.dim_hidden], initializer=conv_initializer, dtype=dtype)
            weights['b3_'+str(e)] = tf.Variable(tf.zeros([self.dim_hidden]), name='b3_'+str(e))
            weights['conv4_'+str(e)] = tf.get_variable('conv4_'+str(e), [k, k, self.dim_hidden, self.dim_hidden], initializer=conv_initializer, dtype=dtype)
            weights['b4_'+str(e)] = tf.Variable(tf.zeros([self.dim_hidden]), name='b4_'+str(e))

            if e==0: # expert0 as the gating scope
                if FLAGS.datasource == 'miniimagenet':
                    # assumes max pooling
                    weights['w5_'+str(e)] = tf.get_variable('w5_'+str(e), [self.dim_hidden*5*5, FLAGS.num_mixtures], initializer=fc_initializer)
                    weights['b5_'+str(e)] = tf.Variable(tf.zeros([FLAGS.num_mixtures]), name='b5_'+str(e))
                else:
                    weights['w5_'+str(e)] = tf.Variable(tf.random_normal([self.dim_hidden, FLAGS.num_mixtures]), name='w5_'+str(e))
                    weights['b5_'+str(e)] = tf.Variable(tf.zeros([FLAGS.num_mixtures]), name='b5_'+str(e))
            else: # expert1~N as the expert scope
                if FLAGS.datasource == 'miniimagenet':
                    # assumes max pooling
                    weights['w5_'+str(e)] = tf.get_variable('w5_'+str(e), [self.dim_hidden*5*5, self.dim_output], initializer=fc_initializer)
                    weights['b5_'+str(e)] = tf.Variable(tf.zeros([self.dim_output]), name='b5_'+str(e))
                else:
                    weights['w5_'+str(e)] = tf.Variable(tf.random_normal([self.dim_hidden, self.dim_output]), name='w5_'+str(e))
                    weights['b5_'+str(e)] = tf.Variable(tf.zeros([self.dim_output]), name='b5_'+str(e))
        
        print weights.keys()
        return weights

    def forward_conv(self, inp, weights, index=-1, reuse=False, scope=''):
        # reuse is for the normalization parameters.
        channels = self.channels
        inp = tf.reshape(inp, [-1, self.img_size, self.img_size, channels])
        
        e = index
        hidden1 = conv_block(inp, weights['conv1_'+str(e)], weights['b1_'+str(e)], reuse, scope=str(e)+'_0')
        hidden2 = conv_block(hidden1, weights['conv2_'+str(e)], weights['b2_'+str(e)], reuse, scope=str(e)+'_1')
        hidden3 = conv_block(hidden2, weights['conv3_'+str(e)], weights['b3_'+str(e)], reuse, scope=str(e)+'_2')
        hidden4 = conv_block(hidden3, weights['conv4_'+str(e)], weights['b4_'+str(e)], reuse, scope=str(e)+'_3')

        if FLAGS.datasource == 'miniimagenet':
            # last hidden layer is 6x6x64-ish, reshape to a vector
            hidden4 = tf.reshape(hidden4, [-1, np.prod([int(dim) for dim in hidden4.get_shape()[1:]])])
        else:
            hidden4 = tf.reduce_mean(hidden4, [1, 2])

        output = tf.matmul(hidden4, weights['w5_'+str(e)]) + weights['b5_'+str(e)]

        return output

