from interpret_text.common.utils_classical import plot_local_imp
from interpret_text.common.constants import ExplainerParams
from interpret_text.three_player_introspective.three_player_introspective_model import ClassifierModule, IntrospectionGeneratorModule, ThreePlayerIntrospectiveModel
from interpret_text.explanation.explanation import _create_local_explanation

import os
import numpy as np
import pandas as pd
from transformers import BertForSequenceClassification

class ThreePlayerIntrospectiveExplainer:
    """
    An explainer for training an explainable neural network for natural language 
    processing and generating rationales used by that network.
    Based on the paper "Rethinking Cooperative Rationalization: Introspective 
    Extraction and Complement Control" by Yu et. al.
    """
    def __init__(self, args, preprocessor, use_bert = True, explainer=None, anti_explainer=None, generator=None, gen_classifier=None):
        """ 
        Initialize the ThreePlayerIntrospectiveExplainer
        """
        self.explainer = explainer
        self.anti_explainer = anti_explainer
        self.generator = generator
        self.gen_classifier = gen_classifier

        self.BERT = use_bert
        if self.BERT:
            args.BERT = True
            args.embedding_dim = 768
            args.hidden_dim = 768
            self.explainer = BertForSequenceClassification.from_pretrained('bert-base-uncased', num_labels = 2, output_hidden_states=False, output_attentions=False)
            self.anti_explainer = BertForSequenceClassification.from_pretrained('bert-base-uncased', num_labels = 2, output_hidden_states=False, output_attentions=False)
            self.gen_classifier = BertForSequenceClassification.from_pretrained('bert-base-uncased', num_labels = 2, output_hidden_states=True, output_attentions=True)
        else:
            args.BERT = False
            word_vocab = preprocessor.word_vocab

        if self.explainer is None:
            self.explainer = ClassifierModule(args, word_vocab)
        if self.anti_explainer is None:
            self.anti_explainer = ClassifierModule(args, word_vocab)
        if self.gen_classifier is None:
            self.gen_classifier = ClassifierModule(args, word_vocab)
        if self.generator is None:
            self.generator = IntrospectionGeneratorModule(args, self.gen_classifier)
        
        self.model = ThreePlayerIntrospectiveModel(args, preprocessor, self.explainer, self.anti_explainer, self.generator, self.gen_classifier)
        self.labels = args.labels

        if args.cuda:
            self.model.cuda()

    def freeze_bert_classifier(self, classifier, entire=False):
        if entire:
            for name, param in classifier.named_parameters():
                param.requires_grad = False
        else:
            for name, param in classifier.named_parameters():
                if "bert.embeddings" in name or ("bert.encoder" in name and "layer.11" not in name):
                    param.requires_grad = False
    
    def train(self, *args, **kwargs):
        return self.fit(*args, **kwargs)

    def fit(self, df_train, df_test, batch_size, num_iteration=40000, pretrain_cls=True, pretrain_train_iters=1000, pretrain_test_iters=200):
        '''
        x_train: list of sentences (strings)
        y_train: list of labels (ex. 0 -- negative and 1 -- positive)
        '''
        # tokenize/otherwise process the lists of sentences
        if self.BERT:
            self.freeze_bert_classifier(self.explainer)
            self.freeze_bert_classifier(self.anti_explainer)
            self.freeze_bert_classifier(self.gen_classifier)

        if pretrain_cls:
            print('pre-training the classifier')
            self.model.pretrain_classifier(df_train, df_test, batch_size, pretrain_train_iters, pretrain_test_iters)
        # encode the list
        self.model.fit(df_train, batch_size, num_iteration)
        
        return self.model

    def predict(self, df_predict):
        self.model.eval()
        batch_x_, batch_m_, batch_y_ = self.model.generate_data(df_predict)
        predict, anti_predict, cls_predict, z, neg_log_prob = self.model.forward(batch_x_, batch_m_)
        predict = predict.detach()
        anti_predict = anti_predict.detach()
        cls_predict = cls_predict.detach()
        z = z.detach()
        predict_dict = {"predict": predict, "anti_predict": anti_predict, "cls_predict": cls_predict, "rationale": z}
        return pd.DataFrame.from_dict(predict_dict)
    
    def score(self, df_test, test_batch_size=200, n_examples_displayed=1):
        """Calculate and store as model attributes:
        Average classification accuracy using rationales (self.avg_accuracy), 
        Average classification accuracy rationale complements (self.anti_accuracy)
        Average sparsity of rationales (self.avg_sparsity)
        
        :param df_test: dataframe containing test data labels, tokens, masks, and counts
        :type df_test: pandas dataframe
        :param n_examples_displayed: number of test examples (with rationale/prediction) to display, default 1
        :type n_examples_displayed: int, optional
        :param test_batch_size: number of examples in each test batch. Default 200.
        :type test_batch_size: int, optional
        """
        self.model.test(df_test, test_batch_size, n_examples_displayed)

    def explain_local(self, sentence, label, preprocessor, hard_importances=True):
        df_label = pd.DataFrame.from_dict({"labels": [label]})
        df_sentence = pd.concat([df_label, preprocessor.preprocess([sentence.lower()])], axis=1)

        x, m, _ = self.model.generate_data(df_sentence)
        predict_df = self.predict(df_sentence)
        predict = predict_df["predict"]
        zs = predict_df["rationale"]
        if not hard_importances:
            zs = self.model.get_z_scores(df_sentence)
            predict_class_idx = np.argmax(predict)
            zs = zs[:, :, predict_class_idx].detach()

        zs = np.array(zs)

        # generate human-readable tokens (individual words)
        seq_len = int(m.sum().item())
        ids = x[:seq_len][0]
        tokens = [preprocessor.reverse_word_vocab[i.item()] for i in ids]

        local_explanation = _create_local_explanation(
            classification=True,
            text_explanation=True,
            local_importance_values=zs[0],
            method=str(type(self.model)),
            model_task="classification",
            features=tokens,
            classes=self.labels,
        )
    
        return local_explanation

    def visualize(self, word_importances, parsed_sentence):
        plot_local_imp(parsed_sentence, word_importances, max_alpha=.5)