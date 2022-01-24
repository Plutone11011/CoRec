#!/usr/bin/env python

import configargparse
import codecs
import torch
#import onmt.model_builder
#import onmt.translate.beam
import onmt.inputters as inputters
from itertools import count
import onmt.opts as opts
#import onmt.decoders.ensemble
from onmt.translate.translator import Translator
from onmt.helpers.model_builder import load_test_model
from onmt.inputters.text_dataset import TextDataset
from onmt.inputters.input_aux import build_dataset_iter


def build_translator(opt, report_score=True, logger=None, out_file=None):
    if out_file is None:
        out_file = codecs.open(opt.output, 'w+', 'utf-8')

    dummy_parser = configargparse.ArgumentParser(description='train.py')
    opts.model_opts(dummy_parser)
    dummy_opt = dummy_parser.parse_known_args([])[0]

    # TODO load model
    model, model_opt = None, None

    # TODO scorer
    # scorer = onmt.translate.GNMTGlobalScorer(opt)
    scorer = None

    translator = DiffTranslator(model, opt, model_opt,
                                global_scorer=scorer, out_file=out_file,
                                report_score=report_score, logger=logger)

    return translator


class DiffTranslator():

    def __init__(self,
                 model,
                 opt,
                 model_opt,
                 global_scorer=None,
                 out_file=None,
                 report_score=True,
                 logger=None):

        self.model = model

        self.gpu = opt.gpu
        self.cuda = opt.gpu > -1

        self.n_best = opt.n_best
        self.max_length = opt.max_length
        self.max_sent_length = opt.max_sent_length
        self.beam_size = opt.beam_size
        self.min_length = opt.min_length
        self.stepwise_penalty = opt.stepwise_penalty
        self.dump_beam = opt.dump_beam
        self.block_ngram_repeat = opt.block_ngram_repeat
        self.ignore_when_blocking = set(opt.ignore_when_blocking)

        self.replace_unk = opt.replace_unk

        self.verbose = opt.verbose
        self.report_bleu = opt.report_bleu
        self.report_rouge = opt.report_rouge
        self.fast = opt.fast
        # TODO copy attention ?
        # self.copy_attn = model_opt.copy_attn

        self.global_scorer = global_scorer
        self.out_file = out_file
        self.report_score = report_score
        self.logger = logger

        self.use_filter_pred = False

        # for debugging
        self.beam_trace = self.dump_beam != ""
        self.beam_accum = None
        if self.beam_trace:
            self.beam_accum = {
                "predicted_ids": [],
                "beam_parent_ids": [],
                "scores": [],
                "log_probs": []}

    def translate(self,
                  src_path=None,
                  src_data_iter=None,
                  tgt_path=None,
                  tgt_data_iter=None,
                  src_dir=None,
                  batch_size=None,
                  attn_debug=False,
                  sem_path=None,
                  src_vocab=None):
        """
        Translate content of `src_data_iter` (if not None) or `src_path`
        and get gold scores if one of `tgt_data_iter` or `tgt_path` is set.

        Note: batch_size must not be None
        Note: one of ('src_path', 'src_data_iter') must not be None

        Args:
            src_path (str): filepath of source data
            src_data_iter (iterator): an interator generating source data
                e.g. it may be a list or an openned file
            tgt_path (str): filepath of target data
            tgt_data_iter (iterator): an interator generating target data
            src_dir (str): source directory path
                (used for Audio and Image datasets)
            batch_size (int): size of examples per mini-batch
            attn_debug (bool): enables the attention logging

        Returns:
            (`list`, `list`)

            * all_scores is a list of `batch_size` lists of `n_best` scores
            * all_predictions is a list of `batch_size` lists
                of `n_best` predictions
        """
        assert src_data_iter is not None or src_path is not None

        if batch_size is None:
            raise ValueError("batch_size must be set")

        test_dataset = TextDataset(src_path, tgt_path)
        vocab = torch.load(src_vocab) # need to create vocab for test set
        test_loader = build_dataset_iter(test_dataset, vocab, batch_size)

        if self.cuda:
            cur_device = "cuda"
        else:
            cur_device = "cpu"

        # TODO builder
        builder = None
        #
        # Statistics
        counter = count(1)
        pred_score_total, pred_words_total = 0, 0
        gold_score_total, gold_words_total = 0, 0

        all_scores = []
        all_predictions = []

        for batch in test_loader:
            batch_data = self.translate_batch(batch, test_dataset, fast=True, attn_debug=False)
            # TODO translations = builder.from_batch(batch_data)
            translations = None
            for trans in translations:
                all_scores += [trans.pred_scores[:self.n_best]]
                pred_score_total += trans.pred_scores[0]
                pred_words_total += len(trans.pred_sents[0])
                if tgt_path is not None:
                    gold_score_total += trans.gold_score
                    gold_words_total += len(trans.gold_sent) + 1

                n_best_preds = [" ".join(pred)
                                for pred in trans.pred_sents[:self.n_best]]
                all_predictions += [n_best_preds]
                self.out_file.write('\n'.join(n_best_preds) + '\n')
                self.out_file.flush()

        return all_scores, all_predictions


    def translate_batch(self, batch, data, attn_debug, fast=False):
        """
        Translate a batch of sentences.

        Mostly a wrapper around :obj:`Beam`.

        Args:
           batch (:obj:`Batch`): a batch from a dataset object
           data (:obj:`Dataset`): the dataset object
           fast (bool): enables fast beam search (may not support all features)

        Todo:
           Shouldn't need the original dataset.
        """
        with torch.no_grad():
            if fast:
                # TODO _fast_translate_batch, _translate_batch
                return self._fast_translate_batch(
                    batch,
                    data,
                    self.max_length,
                    min_length=self.min_length,
                    n_best=self.n_best,
                    return_attention=attn_debug or self.replace_unk)
            else:
                return self._translate_batch(batch, data)