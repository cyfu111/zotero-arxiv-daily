import numpy as np
from sentence_transformers import SentenceTransformer
from digest import Paper
from datetime import datetime

def rerank_paper(candidate:list[Paper],corpus:list[dict],model:str='avsolatorio/GIST-small-Embedding-v0') -> list[Paper]:
    encoder = SentenceTransformer(model)
    #sort corpus by date, from newest to oldest
    corpus = sorted(corpus,key=lambda x: datetime.strptime(x['data'].get('dateAdded', '1970-01-01T00:00:00Z'), '%Y-%m-%dT%H:%M:%SZ'),reverse=True)
    time_decay_weight = 1 / (1 + np.log10(np.arange(len(corpus)) + 1))
    time_decay_weight = time_decay_weight / time_decay_weight.sum()
    corpus_feature = encoder.encode([paper['data'].get('abstractNote') or paper['data'].get('title', '') for paper in corpus])
    candidate_feature = encoder.encode([paper.summary for paper in candidate])
    sim = encoder.similarity(candidate_feature,corpus_feature) # [n_candidate, n_corpus]
    scores = (sim * time_decay_weight).sum(axis=1) * 10 # [n_candidate]
    for s,c in zip(scores,candidate):
        c.score = s.item()
    candidate = sorted(candidate,key=lambda x: x.score,reverse=True)
    return candidate