import os
from typing import List, Optional, Tuple
from shutil import copyfile
import sentencepiece as spm
import warnings
import logging
import json
import multiprocessing
from collections import Counter
from typing import Collection, Callable, Dict
from tokenizers import NormalizedString, PreTokenizedString
from transformers.tokenization_utils import PreTrainedTokenizer
from tokenizers import Tokenizer, pre_tokenizers, models
from pythainlp.tokenize import word_tokenize, syllable_tokenize
from pythainlp.corpus import thai_syllables, thai_words
from pythainlp.util.trie import Trie
from functools import partial
from helper import get_file_size


logger = logging.getLogger()

VOCAB_FILES_NAMES = {"vocab_file": "sentencepiece.bpe.model"}

PRETRAINED_POSITIONAL_EMBEDDINGS_SIZES = {
    "th-roberta-base": 514,
}
SPIECE_UNDERLINE = '▁'
SPACE_TOKEN = "<_>"
DEPRECATED_SPACE_TOKEN = '<th_roberta_space_token>'
ADDITIONAL_SPECIAL_TOKENS = ['<s>', '<pad>', '</s>', '<unk>', '<mask>', SPACE_TOKEN, '\n']

PRE_TOKENIZERS_MAP = {'newmm': partial(
    word_tokenize,
    custom_dict=Trie(frozenset(set(thai_words()).union(set(ADDITIONAL_SPECIAL_TOKENS))))
    ),
                      'syllable': partial(
    syllable_tokenize,
    custom_dict=Trie(frozenset(set(thai_syllables()).union(set(ADDITIONAL_SPECIAL_TOKENS))))
    )}

_nb_cores = multiprocessing.cpu_count()


class CustomPreTokenizer:
    def __init__(self, pre_tokenize_func: Callable):
        self.pre_tokenize_func = pre_tokenize_func

    def split(
        self, n: int, normalized_string: NormalizedString
    ) -> Collection[NormalizedString]:
        break_i = []
        total_i = 0
        for word in self.pre_tokenize_func(str(normalized_string)):
            total_i += len(word)
            break_i.append(total_i)
        splits = []
        last = 0
        for (i, char) in enumerate(str(normalized_string)):
            if i in break_i:
                splits.append(normalized_string[last:i])
                last = i
        splits.append(normalized_string[last:])
        return splits

    def pre_tokenize(self, pretok: PreTokenizedString):
        pretok.split(self.split)


class WordLevelTrainer:
    def __init__(
        self,
        pre_tokenize_func: Callable,
        input_files: str,
        additional_special_tokens: Collection[str],
        vocab_size: int = None,
        vocab_min_freq: int = None,
        progress: bool = True
    ):
        self.pre_tokenize_func = pre_tokenize_func
        self.vocab_size = vocab_size
        self.special_tokens = additional_special_tokens
        self.input_files = input_files
        self.vocab = None
        self.freq = None
        self.vocab_min_freq = vocab_min_freq
        self.progress = progress
        if self.vocab_min_freq is not None and self.vocab_size is not None:
            raise AttributeError('use only vocab_min_freq or vocab_size')

    def count_one(self, fname: str) -> Counter:
        with open(fname, "r") as f:
            file_size = get_file_size(f)
            words = []
            i = 0
            while True:
                line = f.readline()
                if line:
                    line = line.strip()
                    if len(line) > 0 and not line.isspace():
                        words.extend(self.pre_tokenize_func(line))
                else:
                    break
                i += 1
                if self.progress and i % 5000 == 0:
                    print(f'\rProcessed {f.tell() / file_size * 100:.2f}%',
                          flush=True, end=' ')
        return Counter(words)

    def count_parallel(self, nb_cores: int = _nb_cores) -> Dict[(str, int)]:
        counters = [self.count_one(fname) for fname in self.input_files]
        # with multiprocessing.Pool(nb_cores) as pool:
        #     counters = pool.map(self.count_one, self.input_files)
        counter_all = sum(counters, Counter())
        # Remove special token from counter_all since this will
        # interfere with vocabulary creation later
        # for example if only '<s>' is in counter and addtional tokens = ['<s>']
        # the return vocab will be {'<s>': 1} instead of expected {'<s>': 0}
        # if we didnt remove '<s>' from counter_all
        special_tok_freq = {}
        for tok in self.special_tokens:
            if tok in counter_all:
                special_tok_freq[tok] = counter_all[tok]
                del counter_all[tok]
        if self.vocab_size is not None:
            counter_all.most_common(self.vocab_size)
        else:
            counter_all = [(key, value) for key, value in counter_all.items()
                           if value >= self.vocab_min_freq]
        self.freq = [(tok, special_tok_freq.get(tok, 0))
                     for tok in self.special_tokens] + counter_all
        self.vocab = dict((c[0], i) for i, c in enumerate(self.freq))
        return self.vocab

    def save_vocab(self, output_path: str):
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(self.vocab, f)


class ThaiRobertaTokenizer(PreTrainedTokenizer):
    """
    Adapted from :class:`~transformers.CamembertTokenizer`. Construct a
    Thai Roberta tokenizer. Based on `SentencePiece <https://github.com/google/sentencepiece>`__.

    This tokenizer inherits from :class:`~transformers.PreTrainedTokenizer` which contains most of the main methods.
    Users should refer to this superclass for more information regarding those methods.

    Args:
        vocab_file (:obj:`str`):
            `SentencePiece <https://github.com/google/sentencepiece>`__ file (generally has a `.spm` extension) that
            contains the vocabulary necessary to instantiate a tokenizer.
        bos_token (:obj:`str`, `optional`, defaults to :obj:`"<s>"`):
            The beginning of sequence token that was used during pretraining. Can be used a sequence classifier token.

            .. note::

                When building a sequence using special tokens, this is not the token that is used for the beginning
                of sequence. The token used is the :obj:`cls_token`.
        eos_token (:obj:`str`, `optional`, defaults to :obj:`"</s>"`):
            The end of sequence token.

            .. note::

                When building a sequence using special tokens, this is not the token that is used for the end
                of sequence. The token used is the :obj:`sep_token`.
        sep_token (:obj:`str`, `optional`, defaults to :obj:`"</s>"`):
            The separator token, which is used when building a sequence from multiple sequences, e.g. two sequences
            for sequence classification or for a text and a question for question answering.
            It is also used as the last token of a sequence built with special tokens.
        cls_token (:obj:`str`, `optional`, defaults to :obj:`"<s>"`):
            The classifier token which is used when doing sequence classification (classification of the whole
            sequence instead of per-token classification). It is the first token of the sequence when built with
            special tokens.
        unk_token (:obj:`str`, `optional`, defaults to :obj:`"<unk>"`):
            The unknown token. A token that is not in the vocabulary cannot be converted to an ID and is set to be this
            token instead.
        pad_token (:obj:`str`, `optional`, defaults to :obj:`"<pad>"`):
            The token used for padding, for example when batching sequences of different lengths.
        mask_token (:obj:`str`, `optional`, defaults to :obj:`"<mask>"`):
            The token used for masking values. This is the token used when training this model with masked language
            modeling. This is the token which the model will try to predict.
        additional_special_tokens (:obj:`List[str]`, `optional`, defaults to :obj:`["<s>NOTUSED", "</s>NOTUSED"]`):
            Additional special tokens used by the tokenizer.

    Attributes:
        sp_model (:obj:`SentencePieceProcessor`):
            The `SentencePiece` processor that is used for every conversion (string, tokens and IDs).
    """

    vocab_files_names = VOCAB_FILES_NAMES
    max_model_input_sizes = PRETRAINED_POSITIONAL_EMBEDDINGS_SIZES
    model_input_names = ["attention_mask"]

    def __init__(
        self,
        vocab_file,
        bos_token="<s>",
        eos_token="</s>",
        sep_token="</s>",
        cls_token="<s>",
        unk_token="<unk>",
        pad_token="<pad>",
        mask_token="<mask>",
        additional_special_tokens=[SPACE_TOKEN],
        **kwargs
    ):
        super().__init__(
            bos_token=bos_token,
            eos_token=eos_token,
            unk_token=unk_token,
            sep_token=sep_token,
            cls_token=cls_token,
            pad_token=pad_token,
            mask_token=mask_token,
            additional_special_tokens=additional_special_tokens,
            **kwargs,
        )
        self.sp_model = spm.SentencePieceProcessor()
        self.sp_model.Load(str(vocab_file))
        self.vocab_file = vocab_file


    def build_inputs_with_special_tokens(
        self, token_ids_0: List[int], token_ids_1: Optional[List[int]] = None
    ) -> List[int]:
        """
        Build model inputs from a sequence or a pair of sequence for sequence classification tasks
        by concatenating and adding special tokens.
        An CamemBERT sequence has the following format:

        - single sequence: ``<s> X </s>``
        - pair of sequences: ``<s> A </s></s> B </s>``

        Args:
            token_ids_0 (:obj:`List[int]`):
                List of IDs to which the special tokens will be added.
            token_ids_1 (:obj:`List[int]`, `optional`):
                Optional second list of IDs for sequence pairs.

        Returns:
            :obj:`List[int]`: List of `input IDs <../glossary.html#input-ids>`__ with the appropriate special tokens.
        """

        if token_ids_1 is None:
            return [self.cls_token_id] + token_ids_0 + [self.sep_token_id]
        cls = [self.cls_token_id]
        sep = [self.sep_token_id]
        return cls + token_ids_0 + sep + sep + token_ids_1 + sep

    def get_special_tokens_mask(
        self, token_ids_0: List[int], token_ids_1: Optional[List[int]] = None, already_has_special_tokens: bool = False
    ) -> List[int]:
        """
        Retrieve sequence ids from a token list that has no special tokens added. This method is called when adding
        special tokens using the tokenizer ``prepare_for_model`` method.

        Args:
            token_ids_0 (:obj:`List[int]`):
                List of IDs.
            token_ids_1 (:obj:`List[int]`, `optional`):
                Optional second list of IDs for sequence pairs.
            already_has_special_tokens (:obj:`bool`, `optional`, defaults to :obj:`False`):
                Whether or not the token list is already formatted with special tokens for the model.

        Returns:
            :obj:`List[int]`: A list of integers in the range [0, 1]: 1 for a special token, 0 for a sequence token.
        """
        if already_has_special_tokens:
            if token_ids_1 is not None:
                raise ValueError(
                    "You should not supply a second sequence if the provided sequence of "
                    "ids is already formated with special tokens for the model."
                )
            return list(map(lambda x: 1 if x in [self.sep_token_id, self.cls_token_id] else 0, token_ids_0))

        if token_ids_1 is None:
            return [1] + ([0] * len(token_ids_0)) + [1]
        return [1] + ([0] * len(token_ids_0)) + [1, 1] + ([0] * len(token_ids_1)) + [1]

    def create_token_type_ids_from_sequences(
        self, token_ids_0: List[int], token_ids_1: Optional[List[int]] = None
    ) -> List[int]:
        """
        Create a mask from the two sequences passed to be used in a sequence-pair classification task.
        CamemBERT, like RoBERTa, does not make use of token type ids, therefore a list of zeros is returned.

        Args:
            token_ids_0 (:obj:`List[int]`):
                List of IDs.
            token_ids_1 (:obj:`List[int]`, `optional`):
                Optional second list of IDs for sequence pairs.

        Returns:
            :obj:`List[int]`: List of zeros.
        """
        sep = [self.sep_token_id]
        cls = [self.cls_token_id]

        if token_ids_1 is None:
            return len(cls + token_ids_0 + sep) * [0]
        return len(cls + token_ids_0 + sep + sep + token_ids_1 + sep) * [0]

    @property
    def vocab_size(self):
        return len(self.sp_model)

    def get_vocab(self):
        vocab = {self.convert_ids_to_tokens(i): i for i in range(self.vocab_size)}
        return vocab

    def _tokenize(self, text):
        return self.sp_model.EncodeAsPieces(text)

    def _convert_token_to_id(self, token):
        """ Converts a token (str) in an id using the vocab. """
        return self.sp_model.PieceToId(token)

    def _convert_id_to_token(self, index):
        """Converts an index (integer) in a token (str) using the vocab."""
        return self.sp_model.IdToPiece(index)

    def __getstate__(self):
        state = self.__dict__.copy()
        state["sp_model"] = None
        return state

    def __setstate__(self, d):
        self.__dict__ = d
        self.sp_model = spm.SentencePieceProcessor()
        self.sp_model.Load(self.vocab_file)

    def convert_tokens_to_string(self, tokens):
        """Converts a sequence of tokens (strings for sub-words) in a single string."""
        out_string = "".join(tokens).replace(SPIECE_UNDERLINE, "\n").strip()
        return out_string

    def save_vocabulary(self, save_directory: str, filename_prefix: Optional[str] = None) -> Tuple[str]:
        if not os.path.isdir(save_directory):
            logger.error("Vocabulary path ({}) should be a directory".format(save_directory))
            return
        out_vocab_file = os.path.join(
            save_directory, (filename_prefix + "-" if filename_prefix else "") + VOCAB_FILES_NAMES["vocab_file"]
        )

        if os.path.abspath(self.vocab_file) != os.path.abspath(out_vocab_file):
            copyfile(self.vocab_file, out_vocab_file)

        return (out_vocab_file,)

    def prepare_for_tokenization(self, text, is_split_into_words=False, **kwargs):
        if "is_pretokenized" in kwargs:
            warnings.warn(
                "`is_pretokenized` is deprecated and will be removed in a future version, use `is_split_into_words` instead.",
                FutureWarning,
            )
            is_split_into_words = kwargs.pop("is_pretokenized")

        # replace empty space with special space token

        text = text.replace(' ', SPACE_TOKEN)

        return (text, kwargs)


class ThaiWordsNewmmTokenizer(PreTrainedTokenizer):
    vocab_files_names = {"vocab_file": "newmm.json"}

    def __init__(
        self,
        vocab_file,
        bos_token="<s>",
        eos_token="</s>",
        sep_token="</s>",
        cls_token="<s>",
        unk_token="<unk>",
        pad_token="<pad>",
        mask_token="<mask>",
        additional_special_tokens=ADDITIONAL_SPECIAL_TOKENS,
        **kwargs
    ):
        super().__init__(
            bos_token=bos_token,
            eos_token=eos_token,
            unk_token=unk_token,
            sep_token=sep_token,
            cls_token=cls_token,
            pad_token=pad_token,
            mask_token=mask_token,
            additional_special_tokens=ADDITIONAL_SPECIAL_TOKENS,
            **kwargs,
        )
        pre_tokenizer_func = PRE_TOKENIZERS_MAP['newmm']
        custom_pre_tokenizer = pre_tokenizers.PreTokenizer.custom(
            CustomPreTokenizer(pre_tokenizer_func))
        tokenizer = Tokenizer(models.WordLevel.from_file(vocab_file))
        tokenizer.pre_tokenizer = custom_pre_tokenizer
        self.tokenizer_model = tokenizer
        self.vocab_file = vocab_file

    def build_inputs_with_special_tokens(
        self, token_ids_0: List[int], token_ids_1: Optional[List[int]] = None
    ) -> List[int]:
        """
        Build model inputs from a sequence or a pair of sequence for sequence classification tasks
        by concatenating and adding special tokens.
        An CamemBERT sequence has the following format:

        - single sequence: ``<s> X </s>``
        - pair of sequences: ``<s> A </s></s> B </s>``

        Args:
            token_ids_0 (:obj:`List[int]`):
                List of IDs to which the special tokens will be added.
            token_ids_1 (:obj:`List[int]`, `optional`):
                Optional second list of IDs for sequence pairs.

        Returns:
            :obj:`List[int]`: List of `input IDs <../glossary.html#input-ids>`__ with the appropriate special tokens.
        """
        if token_ids_1 is None:
            return [self.cls_token_id] + token_ids_0 + [self.sep_token_id]
        cls = [self.cls_token_id]
        sep = [self.sep_token_id]
        return cls + token_ids_0 + sep + sep + token_ids_1 + sep

    def get_special_tokens_mask(
        self, token_ids_0: List[int], token_ids_1: Optional[List[int]] = None, already_has_special_tokens: bool = False
    ) -> List[int]:
        """
        Retrieve sequence ids from a token list that has no special tokens added. This method is called when adding
        special tokens using the tokenizer ``prepare_for_model`` method.

        Args:
            token_ids_0 (:obj:`List[int]`):
                List of IDs.
            token_ids_1 (:obj:`List[int]`, `optional`):
                Optional second list of IDs for sequence pairs.
            already_has_special_tokens (:obj:`bool`, `optional`, defaults to :obj:`False`):
                Whether or not the token list is already formatted with special tokens for the model.

        Returns:
            :obj:`List[int]`: A list of integers in the range [0, 1]: 1 for a special token, 0 for a sequence token.
        """
        if already_has_special_tokens:
            if token_ids_1 is not None:
                raise ValueError(
                    "You should not supply a second sequence if the provided sequence of "
                    "ids is already formated with special tokens for the model."
                )
            return list(map(lambda x: 1 if x in [self.sep_token_id, self.cls_token_id] else 0, token_ids_0))

        if token_ids_1 is None:
            return [1] + ([0] * len(token_ids_0)) + [1]
        return [1] + ([0] * len(token_ids_0)) + [1, 1] + ([0] * len(token_ids_1)) + [1]

    def create_token_type_ids_from_sequences(
        self, token_ids_0: List[int], token_ids_1: Optional[List[int]] = None
    ) -> List[int]:
        """
        Create a mask from the two sequences passed to be used in a sequence-pair classification task.
        CamemBERT, like RoBERTa, does not make use of token type ids, therefore a list of zeros is returned.

        Args:
            token_ids_0 (:obj:`List[int]`):
                List of IDs.
            token_ids_1 (:obj:`List[int]`, `optional`):
                Optional second list of IDs for sequence pairs.

        Returns:
            :obj:`List[int]`: List of zeros.
        """
        sep = [self.sep_token_id]
        cls = [self.cls_token_id]

        if token_ids_1 is None:
            return len(cls + token_ids_0 + sep) * [0]
        return len(cls + token_ids_0 + sep + sep + token_ids_1 + sep) * [0]

    @property
    def vocab_size(self):
        return len(self.tokenizer_model.get_vocab())

    def get_vocab(self):
        vocab = {self.convert_ids_to_tokens(i): i for i in range(self.vocab_size)}
        return vocab

    def _tokenize(self, text):
        return self.tokenizer_model.encode(text).tokens

    def _convert_token_to_id(self, token):
        """ Converts a token (str) in an id using the vocab. """
        i = self.tokenizer_model.token_to_id(token)
        if i is None:
            return self.unk_token_id
        else:
            return i

    def _convert_id_to_token(self, index):
        """Converts an index (integer) in a token (str) using the vocab."""
        return self.tokenizer_model.id_to_token(index)

    def __getstate__(self):
        state = self.__dict__.copy()
        state["tokenizer_model"] = None
        return state

    def __setstate__(self, d):
        self.__dict__ = d
        pre_tokenizer_func = Trie(frozenset(set(thai_words()).union(set(ADDITIONAL_SPECIAL_TOKENS))))
        custom_pre_tokenizer = pre_tokenizers.PreTokenizer.custom(
            CustomPreTokenizer(pre_tokenizer_func))
        tokenizer = Tokenizer(models.WordLevel.from_file(self.vocab_file))
        tokenizer.pre_tokenizer = custom_pre_tokenizer
        self.tokenizer_model = tokenizer

    def convert_tokens_to_string(self, tokens):
        """Converts a sequence of tokens (strings for sub-words) in a single string."""
        out_string = "".join(tokens).strip()
        return out_string

    def save_vocabulary(self, save_directory: str, filename_prefix: Optional[str] = None) -> Tuple[str]:
        if not os.path.isdir(save_directory):
            logger.error("Vocabulary path ({}) should be a directory".format(save_directory))
            return
        out_vocab_file = os.path.join(
            save_directory, (filename_prefix + "-" if filename_prefix else "") + VOCAB_FILES_NAMES["vocab_file"]
        )

        if os.path.abspath(self.vocab_file) != os.path.abspath(out_vocab_file):
            copyfile(self.vocab_file, out_vocab_file)

        return (out_vocab_file,)

    def prepare_for_tokenization(self, text, is_split_into_words=False, **kwargs):
        if "is_pretokenized" in kwargs:
            warnings.warn(
                "`is_pretokenized` is deprecated and will be removed in a future version, use `is_split_into_words` instead.",
                FutureWarning,
            )
            is_split_into_words = kwargs.pop("is_pretokenized")

        # replace empty space with special space token

        text = text.replace(' ', SPACE_TOKEN)

        return (text, kwargs)
