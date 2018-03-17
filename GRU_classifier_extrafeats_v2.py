#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Thu Mar  8 17:51:50 2018

GRU CLassifier for text classification
@author: mohsin
"""

import inspect
import numpy as np
#from keras.preprocessing.text import Tokenizer
from keras.optimizers import Adam, RMSprop, Nadam
from keras.models import Model, load_model
from keras.layers import Input, Embedding, SpatialDropout1D, CuDNNGRU, GRU, Bidirectional, Dropout, Dense, PReLU, BatchNormalization
from keras.layers import concatenate, GlobalAveragePooling1D, GlobalMaxPooling1D, Permute, Reshape, merge, Lambda , RepeatVector
from keras.callbacks import ModelCheckpoint
from ZeroMaskedLayer import ZeroMaskedLayer
from AttentionLayer import AttentionLayer
import keras.backend as K
from sklearn.base import BaseEstimator, ClassifierMixin

#%%
class GRUClassifier(BaseEstimator, ClassifierMixin):
    def __init__(self, 
                 max_seq_len=100,
                 embed_vocab_size=100000,
                 embed_dim=300,
                 spatial_dropout=0.2, #Spatial Dropout to be used just after embedxing layer
                 gru_dim=150, #Hidden dimension for GRU cell
                 cudnn=True,
                 bidirectional = True,  #Whether to use bidirectional GRU cell
                 gru_layers=1,
                 attention=False,
                 single_attention_vector=True,
                 apply_before_lstm=False,
                 trainable=False,
                 add_extra_feats=True,
                 extra_feats_dim=0,
                 pooling= 'max_attention', #Type of pooling layer to be applied on GRU output sequences
                                          #Various options for pooling layer are:
                                          # 'max' : GlobalMaxPooling Layer
                                          # 'mean' : GlobalAverage PoolingLayer
                                          # 'attention' : Weighted attention layer
                                          # 'max_attention' : Concatenation to max pooling and attention layer                 
                 fc_dim=256,  #Dimension for fully connected layer
                 fc_dropout=0.2, #Dropout ot be used before fully connected layer
                 fc_layers=1,
                 prelu=True,
                 optimizer= 'adam', #Optimizer to be used
                 out_dim = 6,
                 batch_size=256,  
                 epochs=1,
                 verbose=1,
                 callbacks=None,
                 mask_zero=False, #Mask zero values in embeddings layer; Zero values mostly are result of padding and/or OOV words
                 model_id =None, #To be used for predicting if we are using checkpoints in callbacxk for our model
                 embed_kwargs={}, #Dict of keyword arguments for word embeddings layer
                 gru_kwargs={}, #Dict of keyword arguments for gru layer
                 opt_kwargs={}, #Dict of keyword arguments for optimization algo
                 fc_kwargs={}
                ):
        
        args, _, _, values = inspect.getargvalues(inspect.currentframe())
        values.pop("self")

        for arg, val in values.items():
            setattr(self, arg, val)
    
    def _gru_block(self, x):
        #Learn document encoding using CuDNNGRU and return hidden sequences
        if self.cudnn:
            gru_layer =  CuDNNGRU(self.gru_dim, return_sequences=True, return_state=True, **self.gru_kwargs)
        else:
            gru_layer = GRU(self.gru_dim, return_sequences=True, return_state=True, **self.gru_kwargs)
        
        #Apply bidirectional wrapper if flag is True
        if self.bidirectional:
            enc = Bidirectional(gru_layer, merge_mode="concat")(x)
            x = enc[0]
            state = enc[1]
        else:
            x, state = gru_layer(x)  
        return x, state
    
    #Taken from here: https://github.com/philipperemy/keras-attention-mechanism/blob/master/attention_lstm.py
    # Big Thanks to Author!
    def _attention_3d_block(self, inputs):
        # inputs.shape = (batch_size, time_steps, input_dim)
        input_dim = int(inputs.shape[2])
        a = Permute((2, 1))(inputs)
        a = Reshape((input_dim, self.max_seq_len))(a) # this line is not useful. It's just to know which dimension is what.
        a = Dense(self.max_seq_len, activation='softmax')(a)
        if self.single_attention_vector:
            a = Lambda(lambda x: K.mean(x, axis=1))(a)
            a = RepeatVector(input_dim)(a)
        a_probs = Permute((2, 1))(a)
        output_attention_mul = merge([inputs, a_probs], mode='mul')
        return output_attention_mul
    
    def _pool_block(self, x, state):
        #Pool layers
        if self.pooling == 'mean':
            x = GlobalAveragePooling1D()(x)
            x = concatenate([x, state])
            
        if self.pooling == 'max':
            x = GlobalMaxPooling1D()(x)
            x = concatenate([x, state])
            
        if self.pooling == 'attention':
            x = AttentionLayer(self.max_seq_len)(x)
            x = concatenate([x, state])
        
        if self.pooling == "mean_max":
            x1 = GlobalAveragePooling1D()(x)
            x2 = GlobalMaxPooling1D()(x)
            #x3 = AttentionLayer(self.max_seq_len)(x)
            x = concatenate([x1, x2])

        elif self.pooling == 'max_attention':
            #x1 = GlobalAveragePooling1D()(emb)
            x2 = GlobalMaxPooling1D()(x)
            x3 = AttentionLayer(self.max_seq_len)(x)
            x = concatenate([x2, x3, state])
        return x
    
    
    def _fc_block(self, x):
        #Fully connected layer
        x = Dropout(self.fc_dropout)(x)
        x = Dense(self.fc_dim, **self.fc_kwargs)(x)
        if self.prelu:
            x = PReLU()(x)
        return x
        
    def _build_model(self):
        #Set input 
        inp = Input(shape=(self.max_seq_len,))
        
        if self.add_extra_feats:
            assert  self.extra_feats_dim > 0
            inp2 = Input(shape=(self.extra_feats_dim,))
        #word embedding layer
        emb = Embedding(self.embed_vocab_size, self.embed_dim, trainable=self.trainable, **self.embed_kwargs)(inp)

        #mask zero values in embedding if flag is true
        if self.mask_zero:
            emb = ZeroMaskedLayer()(emb)
            
        if self.attention and self.apply_before_lstm:
            emb = self._attention_3d_block(emb)
        
        #Apply spatial dropout to avoid overfitting
        x = SpatialDropout1D(self.spatial_dropout)(emb)
        
        for _ in range(self.gru_layers):
            x, state = self._gru_block(x)
        
        if self.attention and ~self.apply_before_lstm:
            x = self._attention_3d_block(x)
            
        x = self._pool_block(x, state)
        
        if self.add_extra_feats:
            x2 = BatchNormalization()(inp2)
            x = concatenate([x, x2])
            
            
        for _ in range(self.fc_layers):
            x = self._fc_block(x)

        #Classification layer
        x = BatchNormalization()(x)
        out = Dense(self.out_dim, activation="sigmoid")(x)
        
        if self.optimizer == 'adam':
            opt = Adam(**self.opt_kwargs)
            
        if self.optimizer == 'nadam':
            opt = Nadam(**self.opt_kwargs)
            
        elif self.optimizer == 'rmsprop':
            opt = RMSprop(**self.opt_kwargs)
        
        x2 = concatenate([out, inp2])
        x2 = BatchNormalization()(x2)
        out2 = Dense(self.out_dim, activation="sigmoid")(x2)
        if self.add_extra_feats:
            model = Model(inputs=[inp, inp2], outputs=[out, out2])
        else:
            model = Model(inputs=inp, outputs=out)
        model.compile(loss='binary_crossentropy', optimizer=opt, metrics=['accuracy'])
        return model
    
    def fit(self, X, y):
        self.model = self._build_model()
        
        if self.callbacks:
            self.model.fit(X, [y, y], batch_size=self.batch_size, epochs=self.epochs,
                       verbose=self.verbose,
                       callbacks=self.callbacks,
                       shuffle=True)
        else:
            self.model.fit(X, [y,y], batch_size=self.batch_size, epochs=self.epochs,
                       verbose=self.verbose,
                       shuffle=True)
        return self
    
    def predict(self, X, y=None):
        if self.model:
            if np.any(isinstance(c, ModelCheckpoint) for c in self.callbacks):
                self.model.load_weights("Model_"+str(self.model_id)+".check")
            y_hat = self.model.predict(X, batch_size=1024)[0]
        else:
            raise ValueError("Model not fit yet")
        return y_hat
    
