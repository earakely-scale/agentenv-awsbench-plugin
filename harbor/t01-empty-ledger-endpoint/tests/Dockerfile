FROM public.ecr.aws/docker/library/python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
COPY constraints.txt requirements.txt /tests/
RUN pip install --no-cache-dir -c /tests/constraints.txt -r /tests/requirements.txt
COPY . /tests/
WORKDIR /tests
