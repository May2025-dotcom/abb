# Use an official Python runtime as a parent image
FROM python:3.10-slim

# Set the working directory in the container
WORKDIR /app

# Copy the app directory contents into the container
COPY /app /app

# Install any necessary dependencies
RUN pip install --no-cache-dir -r /app/requirements.txt

# Caches and the rate-limit pause are kept here (mounted as a volume)
ENV DATA_DIR=/data
RUN mkdir -p /data

# Expose the port the app runs on
EXPOSE 5078

# Define the command to run the application
CMD ["python", "app.py"]
