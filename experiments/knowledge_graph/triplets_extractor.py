import math
from .knowledge_base import KnowledgeBase


class KGTripletExtractor:
    """
    A class to perform knowledge graph triplet extraction from text using a pre-trained model.

    Methods:
        text_to_wiki_kb(text, model=None, tokenizer=None, device='cpu', span_length=512,
                        article_title=None, article_publish_date=None, verbose=False):
            Extract knowledge graph triplets from text and create a KnowledgeBase (KB) containing entities and relations.
    """

    @staticmethod
    def text_to_wiki_kb(
        text,
        model=None,
        tokenizer=None,
        device="mps:0",
        span_length=512,
        article_title=None,
        article_publish_date=None,
        verbose=False,
    ):
        """
        Extract knowledge graph triplets from text and create a KnowledgeBase (KB) containing entities and relations.

        Args:
            text (str): The input text from which triplets will be extracted.
            model (AutoModelForSeq2SeqLM, optional): The pre-trained model for triplet extraction. Defaults to None.
            tokenizer (AutoTokenizer, optional): The tokenizer for the model. Defaults to None.
            device (str, optional): The device to run the model on (e.g., 'cpu', 'cuda'). Defaults to 'cpu'.
            span_length (int, optional): The maximum span length for input text segmentation. Defaults to 512.
            article_title (str, optional): The title of the article containing the input text. Defaults to None.
            article_publish_date (str, optional): The publish date of the article. Defaults to None.
            verbose (bool, optional): Whether to enable verbose mode for debugging. Defaults to False.

        Returns:
            KnowledgeBase: A KnowledgeBase (KB) containing extracted entities, relations, and sources.

        """
        from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
        import torch

        if not any([model, tokenizer]):
            tokenizer = AutoTokenizer.from_pretrained("Babelscape/rebel-large")
            model = AutoModelForSeq2SeqLM.from_pretrained("Babelscape/rebel-large")
            model.to(device)

        inputs = tokenizer([text], return_tensors="pt")

        num_tokens = len(inputs["input_ids"][0])
        if verbose:
            print(f"Input has {num_tokens} tokens")
        num_spans = math.ceil(num_tokens / span_length)
        if verbose:
            print(f"Input has {num_spans} spans")
        overlap = math.ceil(
            (num_spans * span_length - num_tokens) / max(num_spans - 1, 1)
        )
        spans_boundaries = []
        start = 0
        for i in range(num_spans):
            spans_boundaries.append(
                [start + span_length * i, start + span_length * (i + 1)]
            )
            start -= overlap
        if verbose:
            print(f"Span boundaries are {spans_boundaries}")

        # transform input with spans
        tensor_ids = [
            inputs["input_ids"][0][boundary[0] : boundary[1]]
            for boundary in spans_boundaries
        ]
        tensor_masks = [
            inputs["attention_mask"][0][boundary[0] : boundary[1]]
            for boundary in spans_boundaries
        ]

        inputs = {
            "input_ids": torch.stack(tensor_ids).to(device),
            "attention_mask": torch.stack(tensor_masks).to(device),
        }

        # generate relations
        num_return_sequences = 3
        gen_kwargs = {
            "max_length": 256,
            "length_penalty": 0,
            "num_beams": 3,
            "num_return_sequences": num_return_sequences,
        }
        generated_tokens = model.generate(
            **inputs,
            **gen_kwargs,
        )

        # decode relations
        decoded_preds = tokenizer.batch_decode(
            generated_tokens, skip_special_tokens=False
        )

        # create kb
        kb = KnowledgeBase()
        i = 0
        for sentence_pred in decoded_preds:
            current_span_index = i // num_return_sequences
            relations = KnowledgeBase.extract_relations_from_model_output(sentence_pred)
            for relation in relations:
                relation["meta"] = {
                    "article_url": {"spans": [spans_boundaries[current_span_index]]}
                }
                kb.add_relation(relation, article_title, article_publish_date)
            i += 1
        return kb
