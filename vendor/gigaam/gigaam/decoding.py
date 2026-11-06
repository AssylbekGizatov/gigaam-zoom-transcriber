# Derived from salute-developers/GigaAM, commit 7447938d791c4f3e643386ee22c33777004293a5.
# Copyright (c) 2024 GigaChat Team. MIT; see ../LICENSE and ../README.md.
from typing import List, Optional, Tuple
import torch
from torch import Tensor
from .decoder import CTCHead

class Tokenizer:
    """
    Tokenizer for converting between text and token IDs.
    The tokenizer can operate either character-wise or using a pre-trained SentencePiece model.
    """

    def __init__(self, vocab: List[str], model_path: Optional[str] = None):
        self.charwise = model_path is None
        if self.charwise:
            self.vocab = vocab
        else:
            raise ValueError("This offline runtime supports character vocabularies only.")

    def decode(self, tokens: List[int]) -> str:
        """
        Convert a list of token IDs back to a string.
        """
        if self.charwise:
            return "".join(self.vocab[tok] for tok in tokens)
        return self.model.decode(tokens)

    def __len__(self):
        """
        Get the total number of tokens in the vocabulary.
        """
        return len(self.vocab) if self.charwise else len(self.model)

    def id_to_str(self, token_id: int) -> str:
        """
        Convert a single token ID to its string representation.
        """
        if self.charwise:
            return self.vocab[token_id]
        return self.model.IdToPiece(token_id)

class CTCGreedyDecoding:
    """
    Class for performing greedy decoding of CTC outputs.
    """

    def __init__(self, vocabulary: List[str], model_path: Optional[str] = None):
        self.tokenizer = Tokenizer(vocabulary, model_path)
        self.blank_id = len(self.tokenizer)

    @torch.inference_mode()
    def decode(
        self,
        head: "CTCHead",
        encoded: Tensor,
        lengths: Tensor,
    ) -> List[Tuple[str, List[int], List[int]]]:
        """
        CTC greedy decode: returns (text, token_ids, token_frames) per sample.
        Token frames are time indices (0..T-1) where a token is emitted.
        """
        log_probs = head(encoder_output=encoded)
        C = log_probs.shape[-1]
        assert (
            C == len(self.tokenizer) + 1
        ), f"Num classes {C} != len(vocab)+1 {len(self.tokenizer) + 1}"
        labels = log_probs.argmax(dim=-1)

        B, T = labels.shape
        device = labels.device
        lengths = lengths.to(device=device).clamp(min=0, max=T)

        skip_mask = labels != self.blank_id
        skip_mask[:, 1:] &= labels[:, 1:] != labels[:, :-1]

        time = torch.arange(T, device=device)[None, :]
        skip_mask &= time < lengths[:, None]

        idx = skip_mask.nonzero(as_tuple=False)
        batch_idx = idx[:, 0]
        token_frames_flat = idx[:, 1]
        token_ids_flat = labels[skip_mask]

        counts = torch.bincount(batch_idx, minlength=B).cpu().tolist()
        ids_splits = token_ids_flat.cpu().split(counts)
        fr_splits = token_frames_flat.cpu().split(counts)

        return [
            (self.tokenizer.decode(ids.tolist()), ids.tolist(), fr.tolist())
            for ids, fr in zip(ids_splits, fr_splits)
        ]
