"""Optional relevance scoring; credentials and model belong to the deployment."""

import math
import os
from urllib.parse import urlparse


class RerankerProviderError(ValueError):
    """Safe diagnostic code without provider bodies, URLs or credentials."""
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class RerankerClient:
    def __init__(self, endpoint, model, api_key_env=None, api_key=None):
        url = urlparse(endpoint)
        local_http = url.scheme == 'http' and url.hostname in ('localhost','127.0.0.1','::1')
        if (url.scheme != "https" and not local_http) or not url.hostname or url.username or url.password:
            raise ValueError("Reranker endpoint must use HTTPS without embedded credentials")
        self.endpoint, self.model, self.api_key_env = endpoint, model, api_key_env
        self.api_key = api_key

    def __call__(self, text, documents, *, client=None):
        import httpx
        if not documents:
            return {}
        key = self.api_key if self.api_key is not None else os.environ.get(self.api_key_env or '')
        if self.api_key is None and not key:
            raise ValueError(f"Set the configured reranker credential environment variable: {self.api_key_env}")
        payload = {"model": self.model, "query": text,
                   "documents": [doc.get('rerank_text', f"{doc['title']}\n{doc['body']}") for doc in documents],
                   "top_n": len(documents), "return_documents": False}
        owned = client is None
        client = client or httpx.Client(timeout=20, follow_redirects=False)
        try:
            response = client.post(self.endpoint, json=payload, headers={"Authorization": f"Bearer {key}"} if key else {})
            if response.status_code != 200:
                raise RerankerProviderError(f'http_{response.status_code}', f"Reranker provider returned HTTP {response.status_code}; response body omitted")
            scores = {}
            for item in response.json()["results"]:
                index, score = item["index"], item["relevance_score"]
                if type(index) is not int or not 0 <= index < len(documents):
                    raise ValueError("Reranker returned an invalid document index")
                ref = documents[index]["ref"]
                if ref in scores or type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
                    raise ValueError("Reranker returned duplicate results or an invalid relevance score")
                scores[ref] = score
            return scores
        except httpx.TimeoutException:
            raise RerankerProviderError('timeout', "Reranker provider request timed out") from None
        except httpx.HTTPError:
            raise RerankerProviderError('request_failed', "Reranker provider request failed") from None
        finally:
            if owned:
                client.close()
