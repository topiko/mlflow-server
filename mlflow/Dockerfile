FROM python:3.10-slim@sha256:fd76ade0c607f27677bc04be3c60749f400eedc941d9e72967e19a4cedff80c2

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && pip check
