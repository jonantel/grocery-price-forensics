FROM public.ecr.aws/lambda/python:3.12

RUN dnf update -y && dnf install -y mesa-libGL && dnf clean all
RUN pip install --no-cache-dir boto3 opencv-python-headless numpy pandas pyarrow rapidfuzz

COPY src/ ${LAMBDA_TASK_ROOT}/

CMD [ "frame_extractor.handler" ]
