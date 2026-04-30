"""
nim-agents-ops api/lib/bigquery_client.py

thin wrapper around google.cloud.bigquery, lazily instantiated. each agent
calls `get_client()` once and reuses for the duration of a run.

billing project defaults to noonbinimops (matches nim-agents-sc) — override
with env BQ_BILLING_PROJECT.
"""
import os

_CLIENT = None


def get_client():
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    from google.cloud import bigquery
    project = os.environ.get("BQ_BILLING_PROJECT", "noonbinimops")
    _CLIENT = bigquery.Client(project=project)
    return _CLIENT


def run(sql, params=None, **kwargs):
    """run a query, return list of dicts. accepts query parameters."""
    from google.cloud import bigquery
    client = get_client()
    cfg = None
    if params:
        bq_params = []
        for k, v in params.items():
            if isinstance(v, int):
                bq_params.append(bigquery.ScalarQueryParameter(k, "INT64", v))
            elif isinstance(v, float):
                bq_params.append(bigquery.ScalarQueryParameter(k, "FLOAT64", v))
            else:
                bq_params.append(bigquery.ScalarQueryParameter(k, "STRING", str(v)))
        cfg = bigquery.QueryJobConfig(query_parameters=bq_params)
    job = client.query(sql, job_config=cfg)
    return [dict(r) for r in job.result(**kwargs)]
