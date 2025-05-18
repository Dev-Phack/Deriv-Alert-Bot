FROM python:3.13-alpine
WORKDIR /code
COPY . /code
RUN pip install --no-cache-dir --upgrade -r /code/requirements.txt
CMD ["python3", "main.py"]