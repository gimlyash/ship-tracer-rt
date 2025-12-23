FROM quay.io/centos/centos:stream9

RUN dnf install -y python3.11 python3.11-pip \
    gcc \
    gcc-c++ \
    postgresql-libs \
    postgresql-devel \
    && dnf clean all

WORKDIR /app

COPY requirements.txt .
RUN pip3.11 install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

EXPOSE 8000

CMD ["python3.11", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
