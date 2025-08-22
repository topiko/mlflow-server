FROM python:3.10-slim as base

# Install MLflow 3+
RUN pip install mlflow==3.3.0 boto3 psycopg2-binary

# (Optional) Install tracing support for GenAI workflows
RUN pip install mlflow-tracing
