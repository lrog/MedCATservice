#!/usr/bin/env python
# -*- coding: utf-8 -*-

import logging
import os
from datetime import datetime, timezone

from medcat.cat import CAT
from medcat.cdb import CDB
from medcat.meta_cat import MetaCAT
from medcat.utils.vocab import Vocab


class NlpProcessor:
    """
    This class defines an interface for NLP Processor
    """
    def __init__(self):
        self.log = logging.getLogger(self.__class__.__name__)

    def get_app_info(self):
        pass

    def process_content(self, content):
        pass

    def process_content_bulk(self, content):
        pass

    @staticmethod
    def _get_timestamp():
        """
        Returns the current timestamp in ISO 8601 format. Formatted as "yyyy-MM-dd'T'HH:mm:ss.SSSXXX".
        :return: timestamp string
        """
        return datetime.now(tz=timezone.utc).isoformat(timespec='milliseconds')


class MedCatProcessor(NlpProcessor):
    """"
    MedCAT Processor class is wrapper over MedCAT that implements annotations extractions functionality
    (both single and bulk processing) that can be easily exposed for an API.
    """
    def __init__(self):
        super().__init__()

        self.log.info('Initializing MedCAT processor ...')

        # TODO: use a config file instead of env variables
        #
        self.app_name = 'MedCAT'
        self.app_lang = 'en'
        self.app_version = MedCatProcessor._get_medcat_version()
        self.app_model = os.getenv("APP_MODEL_NAME", 'unknown')

        self.cat = self._create_cat()
        self.cat.spacy_cat.train = os.getenv("APP_TRAINING_MODE", False)

        self.bulk_nproc = int(os.getenv('APP_BULK_NPROC', 8))

        self.log.info('MedCAT processor is ready')

    def get_app_info(self):
        """
        Returns general information about the application
        :return: application information stored as KVPs
        """
        return {'name': self.app_name,
                'language': self.app_lang,
                'version': self.app_version,
                'model': self.app_model}

    def process_content(self, content):
        """
        Processes a single document extracting the annotations.
        :param content: document to be processed, containing 'text' field.
        :return: processing result containing document with extracted annotations stored as KVPs.
        """
        if 'text' not in content:
            error_msg = "'text' field missing in the payload content."
            nlp_result = {'success': False,
                          'errors': [error_msg],
                          'timestamp': NlpProcessor._get_timestamp()
                          }
            return nlp_result, False

        text = content['text']

        # assume an that a blank document is a valid document and process it only
        # when it contains any non-blank characters
        if text is not None and len(text.strip()) > 0:
            entities = self.cat.get_entities(text)
        else:
            entities = []

        nlp_result = {'text': text,
                      'annotations': entities,
                      'success': True,
                      'timestamp': NlpProcessor._get_timestamp()
                      }

        # append the footer
        if 'footer' in content:
            nlp_result['footer'] = content['footer']

        return nlp_result

    def process_content_bulk(self, content):
        """
        Processes an array of documents extracting the annotations.
        :param content: document to be processed, containing 'text' field.
        :return: processing result containing documents with extracted annotations,stored as KVPs.
        """

        # process at least 10 docs per thread and don't bother with starting
        # additional threads when less documents were provided
        min_doc_per_thread = 10
        num_slices = max(1, int(len(content) / min_doc_per_thread))
        batch_size = min(300, num_slices)

        if batch_size >= self.bulk_nproc:
            nproc = self.bulk_nproc
        else:
            batch_size = min_doc_per_thread
            nproc = max(1, num_slices)
            if len(content) > batch_size * nproc:
                nproc += 1

        # use generators both to provide input documents and to provide resulting annotations
        # to avoid too many mem-copies
        invalid_doc_ids = []
        ann_res = self.cat.multi_processing(MedCatProcessor._generate_input_doc(content, invalid_doc_ids),
                                            nproc=nproc, batch_size=batch_size)

        return MedCatProcessor._generate_result(content, ann_res, invalid_doc_ids)

    # helper MedCAT methods
    #
    def _create_cat(self):
        """
        Loads MedCAT resources and creates CAT instance
        """
        if os.getenv("APP_MODEL_VOCAB_PATH") is None:
            raise ValueError("Vocabulary (env: APP_MODEL_VOCAB_PATH) not specified")

        if os.getenv("APP_MODEL_CDB_PATH") is None:
            raise Exception("Concept database (env: APP_MODEL_CDB_PATH) not specified")

        # Vocabulary and Concept Database are mandatory
        self.log.debug('Loading VOCAB ...')
        vocab = Vocab()
        vocab.load_dict(path=os.getenv("APP_MODEL_VOCAB_PATH"))

        self.log.debug('Loading CDB ...')
        cdb = CDB()
        cdb.load_dict(path=os.getenv("APP_MODEL_CDB_PATH"))

        # Apply CUI filter if provided
        if os.getenv("APP_MODEL_CUI_FILTER_PATH") is not None:
            self.log.debug('Applying CDB CUI filter ...')
            with open(os.getenv("APP_MODEL_CUI_FILTER_PATH")) as cui_file:
                all_lines = (line.rstrip() for line in cui_file)
                selected_cuis = [line for line in all_lines if line]  # filter blank lines
                cdb.filter_by_cui(selected_cuis)

        # Meta-annotation models are optional
        meta_models = []
        if os.getenv("APP_MODEL_META_PATH_LIST") is not None:
            self.log.debug('Loading META annotations ...')
            for model_path in os.getenv("APP_MODEL_META_PATH_LIST").split(':'):
                m = MetaCAT(save_dir=model_path)
                m.load()
                meta_models.append(m)

        return CAT(cdb=cdb, vocab=vocab, meta_cats=meta_models)

    # helper generator functions to avoid multiple copies of data
    #
    @staticmethod
    def _generate_input_doc(documents, invalid_doc_idx):
        """
        Generator function returning documents to be processed as a list of tuples:
          (idx, text), (idx, text), ...
        Skips empty documents and reports their ids to the invalid_doc_idx array
        :param documents: array of input documents that contain 'text' field
        :param invalid_doc_idx:  array that will contain invalid document idx
        :return: consecutive tuples of (idx, document)
        """
        for i in range(0, len(documents)):
            # assume the document to be processed only when it is not blank
            if 'text' in documents[i] and documents[i]['text'] is not None and len(documents[i]['text'].strip()) > 0:
                yield i, documents[i]['text']
            else:
                invalid_doc_idx.append(i)

    @staticmethod
    def _generate_result(in_documents, annotations, invalid_doc_idx):
        """
        Generator function merging the resulting annotations with the input documents.
        The result for documents that were invalid will not contain any annotations.
        :param in_documents: array of input documents that contain 'text' field
        :param annotations: array of annotations extracted from documents
        :param invalid_doc_idx: array of invalid document idx
        :return:
        """
        # generate output for valid annotations
        for i in range(len(annotations)):
            res = annotations[i]
            res_idx = res[0]
            in_ct = in_documents[res_idx]

            # parse the result
            out_res = {'text': res[1]["text"],
                       'annotations': res[1]["entities"],
                       'success': True,
                       'timestamp': NlpProcessor._get_timestamp()
                       }
            # append the footer
            if 'footer' in in_ct:
                out_res['footer'] = in_ct['footer']

            yield out_res

        # generate output for invalid documents
        for i in invalid_doc_idx:
            in_ct = in_documents[i]

            out_res = {'text': in_ct["text"],
                       'annotations': [],
                       'success': True,
                       'timestamp': NlpProcessor._get_timestamp()
                       }
            # append the footer
            if 'footer' in in_ct:
                out_res['footer'] = in_ct['footer']

            yield out_res

    @staticmethod
    def _get_medcat_version():
        """
        Returns the version string of the MedCAT module as reported by pip
        :return:
        """
        try:
            import subprocess
            result = subprocess.check_output(['pip', 'show', 'medcat'], universal_newlines=True)
            version_line = list(filter(lambda v: 'Version' in v, result.split('\n')))
            return version_line[0].split(' ')[1]
        except Exception:
            raise Exception("Cannot read the MedCAT library version")
