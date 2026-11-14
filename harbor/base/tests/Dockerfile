FROM {python_image}
COPY constraints.txt requirements.txt /tests/
RUN pip install --no-cache-dir -c /tests/constraints.txt -r /tests/requirements.txt
COPY . /tests/
WORKDIR /tests
