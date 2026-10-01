FROM public.ecr.aws/lambda/python:3.11

# 1. Install system utilities and OCR engines (Poppler + Tesseract)
RUN yum install -y \
    poppler-utils \
    tesseract \
    tesseract-langpack-eng \
    mesa-libGL \
    && yum clean all

# 2. Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3. Copy application handler
COPY parser_lambda.py ${LAMBDA_TASK_ROOT}/

# 4. Set the Lambda entry point
CMD ["parser_lambda.handler"]
