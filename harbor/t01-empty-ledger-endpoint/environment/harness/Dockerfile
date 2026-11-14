FROM public.ecr.aws/docker/library/python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
COPY constraints.txt requirements.txt /opt/awsbench/
RUN pip install --no-cache-dir -c /opt/awsbench/constraints.txt -r /opt/awsbench/requirements.txt
COPY awsbench_harness /opt/awsbench/awsbench_harness
COPY setup.json hook.py verify.py /opt/awsbench/
ENV PYTHONPATH=/opt/awsbench PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /opt/awsbench
CMD ["python", "-m", "awsbench_harness", "serve"]
