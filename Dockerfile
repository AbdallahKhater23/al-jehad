# 1. Use a lightweight Python base
FROM python:3.10-slim

# 2. Install Linux libraries required by DeepFace/OpenCV
RUN apt-get update && apt-get install -y \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# 3. Create a non-root user (Hugging Face security requirement)
RUN useradd -m -u 1000 user
USER user
ENV PATH="/home/user/.local/bin:$PATH"

WORKDIR /app

# 4. Copy the requirements and install them
COPY --chown=user:user requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. Copy your actual code into the container
COPY --chown=user:user . .

# 6. Ensure the temp folder exists so the app doesn't crash on upload
RUN mkdir -p temp local_references

# 7. Expose the Hugging Face port
EXPOSE 7860

# 8. Start the FastAPI server
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"]