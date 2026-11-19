FROM python:3.13-slim
WORKDIR /app
# Baked at build time (release.yml: r<run number>, bootstrap: bootstrap). Never set it in
# the Container App template: the pipeline copies the stable env into the candidate.
ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION
COPY server.py verify.html ./
USER 65532:65532
ENV PYTHONUNBUFFERED=1 PORT=8080
EXPOSE 8080
CMD ["python", "server.py"]
