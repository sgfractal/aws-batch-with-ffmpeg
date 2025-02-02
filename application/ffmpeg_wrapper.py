# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import errno
import json
import logging
import os
import shlex
import subprocess  # nosec B404
import sys
import tempfile
import time

import boto3
import click
from aws import aws_helper
from aws.s3_url import S3Url
from aws_xray_sdk.core import xray_recorder
from botocore.exceptions import ClientError
from ffmpeg_quality_metrics import FfmpegQualityMetrics as ffqm

xray_recorder.configure(sampling=False)
xray_recorder.configure(plugins=["EC2Plugin", "ECSPlugin"])
xray_recorder.configure(context_missing="LOG_ERROR")

LOGLEVEL = os.environ.get("LOGLEVEL", "INFO").upper()
logging.basicConfig(level=LOGLEVEL)
logging.getLogger("aws_xray_sdk").setLevel(LOGLEVEL)


@click.command(name="main")
@click.option("--global_options", help="ffmpeg global options", type=str)
@click.option("--input_file_options", help="ffmpeg input file options", type=str)
@click.option("--input_url", help="Amazon S3 input url", type=str)
@click.option("--output_file_options", help="ffmpeg output file options", type=str)
@click.option("--output_url", help="Amazon S3 output url", type=str)
@click.option("--name", help="Optional name to identify cmd in logs", type=str)
@click.option("--duration", help="Duration of the recording in seconds", type=int)  # Default to 1 hour
@click.option("--stream_address", help="livestream address", type=str)  

def main(
    global_options, input_file_options, input_url, output_file_options, output_url, name, duration, stream_address,
):
    """Python CLI for FFMPEG with Amazon S3 download/upload and video quality
    metrics."""

    aws_region = aws_helper.detect_running_region()
    logging.info("AWS Region : %s", aws_region)
    ssm_client = boto3.client("ssm", region_name=aws_region)
    s3_client = boto3.client("s3", region_name=aws_region)

      # Handling livestream recording if stream_address is provided
    if stream_address:
        record_livestream(stream_address, duration, name, global_options, output_file_options, s3_client, output_url)
        return  # Exit after processing the livestream

    # Arguments validation
    logging.info("global_options: %s", global_options)
    logging.info("input_file_options : %s", input_file_options)
    logging.info("input_url : %s", input_url)
    logging.info("output_file_options : %s", output_file_options)
    logging.info("output_url : %s", output_url)
    logging.info("name : %s", name)
    logging.info("duration : %s", duration)
    logging.info("stream_address : %s", stream_address)

    if global_options == "null":
        global_options = None
    if input_file_options == "null":
        input_file_options = None
    if input_url == "null":
        input_url = None
    if output_file_options == "null":
        output_file_options = None
    if output_url == "null":
        output_url = None
    if name == "null":
        name = None
    if duration == "null":
        duration = None
    if stream_address == "null":
        stream_address = None

    # Get env variables
    aws_batch_job_id = os.getenv("AWS_BATCH_JOB_ID", "local")
    aws_batch_jq_name = os.getenv("AWS_BATCH_JQ_NAME", "local")
    aws_batch_ce_name = os.getenv("AWS_BATCH_CE_NAME", "local")
    s3_bucket_stack = os.getenv("S3_BUCKET", None)
    fsx_lustre_mount_point = os.getenv("FSX_MOUNT_POINT", None)
    logging.info(
        "AWS Batch JobId = %s - AWS Batch Job Queue Name = %s - AWS Batch Compute Env. = %s",
        aws_batch_job_id,
        aws_batch_jq_name,
        aws_batch_ce_name,
    )
    logging.info("Fsx Lustre enabled : %s", fsx_lustre_mount_point is not None)

    # Get AWS parameters
    try:
        parameter = ssm_client.get_parameter(
            Name="/batch-ffmpeg/ffqm", WithDecryption=False
        )
        metrics_flag = parameter["Parameter"]["Value"]

    except ClientError:
        logging.error("metrics flag not found in SSM Parameter")
        metrics_flag = "FALSE"

    # Prepare X-Ray traces
    segment = xray_recorder.begin_segment("batch-ffmpeg-job")
    segment.put_metadata(
        "execution", "ffmpeg-wrapper-" + time.strftime("%Y%m%d-%H%M%S")
    )
    segment.put_annotation("application", "batch-ffmpeg")
    segment.put_annotation("global_options", global_options)
    segment.put_annotation("input_file_options", input_file_options)
    segment.put_annotation("input_url", input_url)
    segment.put_annotation("output_file_options", output_file_options)
    segment.put_annotation("output_url", output_url)
    segment.put_annotation("name", name)
    segment.put_annotation("duration", duration)
    segment.put_annotation("stream_address", stream_address)
    segment.put_annotation("AWS_BATCH_JOB_ID", aws_batch_job_id)
    segment.put_annotation("AWS_BATCH_JQ_NAME", aws_batch_jq_name)
    segment.put_annotation("AWS_BATCH_CE_NAME", aws_batch_ce_name)

    # Prepare media asset inputs from storage
    input_files_path, output_file_path = prepare_assets(
        input_url=input_url,
        output_url=output_url,
        s3_client=s3_client,
        fsx_lustre_mount_point=fsx_lustre_mount_point,
    )

   # ffmpeg command creation
    command_list = ["ffmpeg"]
    if global_options:
        command_list = command_list + shlex.split(global_options)
    if input_url:
        if input_file_options:
            command_list = command_list + shlex.split(input_file_options)
        for file in input_files_path:
            command_list.append("-i")
            command_list.append(file)
    if output_url:
        if output_file_options:
            command_list = command_list + shlex.split(output_file_options)
        command_list.append(output_file_path)

    # ffmpeg execution
    logging.info("ffmpeg command to launch : %s", " ".join(command_list))
    subsegment = xray_recorder.begin_subsegment("cmd-execution")
    subsegment.put_metadata("command", " ".join(command_list))

    p = subprocess.run(
        command_list,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        cwd=None,
        timeout=None,
        check=False,
        encoding=None,
    )  # nosec B603
    if p.returncode != 0:
        logging.error("ffmpeg failed - return code : %d", p.returncode)
        logging.error("ffmpeg failed - output : %s", p.stdout)
        logging.error("ffmpeg failed - error : %s", p.stderr)
        sys.exit(1)
    logging.info("ffmpeg succeeded %d %s %s", p.returncode, p.stdout, p.stderr)
    xray_recorder.end_subsegment()

    if not fsx_lustre_mount_point:
        # Uploading media output to Amazon S3
        s3_output_url = S3Url(output_url)
        try:
            if "%" in s3_output_url.key:
                # Sync output directory
                split = s3_output_url.key.split("/")
                key_output = "/".join(split[:-1])
                sync_dir_to_s3(
                    s3_client,
                    os.path.dirname(output_file_path) + "/",
                    s3_output_url.bucket,
                    key_output,
                )
            else:
                # Upload a file
                upload_file_to_s3(
                    s3_client, output_file_path, s3_output_url.bucket, s3_output_url.key
                )
        except Exception as e:
            logging.error(
                "The app can not upload %s on this S3 bucket (%s - %s)",
                os.path.dirname(output_file_path) + "/",
                s3_output_url.bucket,
                s3_output_url.key,
            )
            logging.error("Upload Error : %s", str(e))
            sys.exit(1)

        logging.info(
            "Done : ffmpeg results uploaded to %s - key_output : %s",
            s3_output_url.bucket,
            s3_output_url.key,
        )

    # Calculate video quality metrics
    try:
        banned_formats = ["%", ".m4a", ".mp3"]
        if metrics_flag == "TRUE" and (
            len(input_files_path) == 1
            and (not any(x in output_url for x in banned_formats))
        ):
            logging.info(
                "Compute video quality metrics - source : %s - destination : %s",
                input_files_path[0],
                output_file_path,
            )
            metrics = quality_metrics(input_files_path[0], output_file_path)
            metrics["AWS_BATCH_JOB_ID"] = aws_batch_job_id
            metrics["AWS_BATCH_JQ_NAME"] = aws_batch_jq_name
            metrics["AWS_BATCH_CE_NAME"] = aws_batch_ce_name
            logging.info("Saving quality metrics in %s", s3_bucket_stack)
            save_qm_s3(s3_client, s3_bucket_stack, metrics)
        else:
            logging.warning(
                "You can't compute quality metrics with this command %s and flag %s",
                name,
                metrics_flag,
            )
    except Exception as e:
        logging.error("Quality Metrics Error : %s", str(e))

    # Clean
    xray_recorder.end_segment()
    sys.exit(0)

def record_livestream(stream_address, duration, name, global_options, output_file_options, s3_client, output_url):
    with tempfile.TemporaryDirectory() as tmpdirname:
        local_output_path = os.path.join(tmpdirname, name)
        
        # Ensure there is a default duration of 2 hours (7200 seconds) if none is provided
        duration_option = f"-t {duration if duration else 7200}"
        command = f"ffmpeg {global_options} -i {stream_address} {duration_option} {output_file_options} {local_output_path}"
        command_list = shlex.split(command)
        
        # Executing ffmpeg command
        try:
            subprocess.run(command_list, check=True)
            logging.info(f"Livestream saved to {local_output_path}")
        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to save livestream: {e}")
            return
        
        # Extract bucket and key from output_url
        s3_output = S3Url(output_url)
        
        # Upload the recorded file to S3
        try:
            s3_client.upload_file(local_output_path, s3_output.bucket, s3_output.key)
            logging.info(f"File uploaded to S3: {output_url}")
        except Exception as e:
            logging.error(f"Failed to upload file to S3: {e}")


def prepare_assets(
    input_url: str, output_url: str, fsx_lustre_mount_point: str, s3_client
):
    """Prepare media assets inputs from storage."""
    s3_output_url = S3Url(output_url)
    s3_inputs = input_url.replace(" ", "").split(",")

    input_files_path = []
    if not fsx_lustre_mount_point:
        # prepare tmp directory
        with tempfile.TemporaryDirectory(prefix="ffmpeg_workdir_") as tmpdirname:
            tmp_dir = tmpdirname + "/"

        output_file_path = tmp_dir + s3_output_url.key
        # Download media assets from S3 bucket
        try:
            input_files_path = download_s3_files(s3_client, s3_inputs, tmp_dir)
        except Exception as e:
            logging.error("Download Error : %s", str(e))
            sys.exit(1)
    else:
        output_file_path = fsx_lustre_mount_point + "/" + s3_output_url.key
        for s3_input in s3_inputs:
            s3_input_url = S3Url(s3_input)
            input_file_path = fsx_lustre_mount_point + "/" + s3_input_url.key
            if not os.path.isfile(input_file_path):
                logging.error("File %s not found on Lustre", input_file_path)
                raise FileNotFoundError(
                    errno.ENOENT, os.strerror(errno.ENOENT), input_file_path
                )
            input_files_path.append(input_file_path)

    os.makedirs(os.path.dirname(output_file_path), exist_ok=True)
    return input_files_path, output_file_path


@xray_recorder.capture("quality-metrics")
def quality_metrics(source: str, destination: str):
    """Compute quality metrics."""
    f = ffqm(source, destination)
    full = f.calculate(metrics=["ssim", "psnr", "vmaf"])
    summary = f.get_global_stats()
    document = xray_recorder.current_subsegment()
    document.put_metadata("quality_metrics", summary)
    return full


def save_qm_s3(s3_client, s3_bucket, document: dict):
    """Save quality metrics on Amazon S3."""
    key = (
        "metrics/ffqm/"
        + time.strftime("year=%Y/month=%b/day=%d")
        + "/"
        + document["AWS_BATCH_JQ_NAME"]
        + "_"
        + document["AWS_BATCH_CE_NAME"]
        + "_"
        + document["AWS_BATCH_JOB_ID"]
        + ".json"
    )
    s3_client.put_object(Bucket=s3_bucket, Key=key, Body=json.dumps(document))


@xray_recorder.capture("download")
def download_s3_files(s3_client, s3_urls: list, destionation_dir):
    """Download a list of Amazon S3 URLs to a directory."""
    files = []
    for s3_url in s3_urls:
        parse = S3Url(s3_url)
        s3_bucket = parse.bucket
        s3_key = parse.key
        path_file = destionation_dir + s3_key
        path_dir = os.path.dirname(path_file)
        os.makedirs(path_dir, exist_ok=True)
        logging.info(
            "Downloading S3 object from (bucket:%s - key:%s) to %s",
            s3_bucket,
            s3_key,
            path_file,
        )
        s3_client.download_file(s3_bucket, s3_key, path_file)
        files.append(path_file)
    return files


@xray_recorder.capture("upload")
def upload_file_to_s3(s3_client, file, s3_bucket, s3_key):
    """Upload file to Amazon S3 bucket."""
    logging.info('Searching "%s" in "%s"', s3_key, s3_bucket)
    # Check if object already exists on S3 and skip upload if it does
    try:
        s3_client.head_object(Bucket=s3_bucket, Key=s3_key)
        # Object found on S3, skip upload
        logging.info("Path found on S3! Skipping %s...", s3_key)
    # Object was not found on S3, proceed to upload
    except ClientError:
        logging.info("Uploading %s in %s", file, s3_key)
        s3_client.upload_file(file, s3_bucket, s3_key)


@xray_recorder.capture("upload")
def sync_dir_to_s3(s3_client, source_dir, s3_bucket, s3_key):
    """Sync `source_dir` directory to Amazon S3 bucket."""
    logging.info("Sync of %s to %s - %s", source_dir, s3_bucket, s3_key)
    for root, dirs, files in os.walk(source_dir):
        for filename in files:
            # construct the full local path
            local_path = os.path.join(root, filename)
            # construct the full S3 path
            relative_path = os.path.relpath(local_path, source_dir)
            s3_path = os.path.join(s3_key, relative_path)
            # Upload file to S3
            upload_file_to_s3(s3_client, local_path, s3_bucket, s3_path)


if __name__ == "__main__":
    main()
