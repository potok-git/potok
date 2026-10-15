FROM python:3.12-alpine

RUN pip install --no-cache-dir \
        "PyYAML>=6.0.2" \
        "Flask>=3.0,<4" \
        "cryptography>=43" \
        "feedparser>=6.0.11" \
        "requests>=2.32"

WORKDIR /app
COPY src /app/src

ENV PYTHONPATH=/app/src \
    POTOK_CONFIG=/etc/potok/config.yaml

EXPOSE 8080
CMD ["python", "-m", "potok.portal"]
