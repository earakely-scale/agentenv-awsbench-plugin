FROM {python_image}
COPY constraints.txt requirements.txt /opt/awsbench/
RUN pip install --no-cache-dir -c /opt/awsbench/constraints.txt -r /opt/awsbench/requirements.txt
COPY awsbench_harness /opt/awsbench/awsbench_harness
COPY setup.json hook.py verify.py /opt/awsbench/
ENV PYTHONPATH=/opt/awsbench PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /opt/awsbench
CMD ["python", "-m", "awsbench_harness", "serve"]
