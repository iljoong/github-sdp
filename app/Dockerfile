FROM python:3.13-slim
WORKDIR /app
COPY server.py .
USER 65532:65532
ENV PYTHONUNBUFFERED=1 PORT=8080
EXPOSE 8080
CMD ["python", "server.py"]
