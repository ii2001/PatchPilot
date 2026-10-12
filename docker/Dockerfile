FROM python:3.11-slim
LABEL patchpilot.sandbox.version="1"
RUN pip install --no-cache-dir pytest==8.4.2 && mkdir -p /workspace /opt/patchpilot
COPY runner.py pytest.ini /opt/patchpilot/
RUN chmod -R a-w /opt/patchpilot && chmod 755 /workspace
ENV PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
USER 65532:65532
WORKDIR /workspace
