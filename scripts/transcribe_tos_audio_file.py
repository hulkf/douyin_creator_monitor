#!/usr/bin/env python3
"""Upload a local audio file to TOS, transcribe it with Volcengine ASR, then clean up."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import correct_transcript
import feishu_transcript_writer
import volcengine_asr
import volcengine_tos


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", help="Local WAV/MP3/OGG audio file.")
    parser.add_argument("--keep-object", action="store_true", help="Do not delete the TOS object after ASR.")
    parser.add_argument("--json-output", help="Optional path for raw ASR JSON.")
    parser.add_argument("--text-output", help="Optional path for extracted transcript text.")
    parser.add_argument("--corrected-text-output", help="Optional path for glossary-corrected transcript text.")
    parser.add_argument("--correction-report-output", help="Optional path for correction report JSON.")
    parser.add_argument("--correction-domain", action="append", help="Glossary domain id for correction.")
    parser.add_argument("--glossary", default=str(correct_transcript.DEFAULT_GLOSSARY_PATH))
    parser.add_argument("--upload-json-output", help="Optional path for TOS upload metadata.")
    parser.add_argument("--url-expires", type=int, help="Pre-signed TOS URL expiry in seconds.")
    parser.add_argument("--feishu-table-id", help="Optional creator-specific Feishu work table ID to write transcript back.")
    parser.add_argument("--feishu-work-id", help="Douyin work/video ID used to locate the Feishu record.")
    parser.add_argument("--feishu-base-token")
    parser.add_argument("--feishu-lark-cli")
    parser.add_argument("--feishu-work-id-field", default="抖音作品ID")
    parser.add_argument("--feishu-transcript-field", default="语音转写全文")
    parser.add_argument("--feishu-dry-run", action="store_true")
    args = parser.parse_args()

    object_key = ""
    try:
        object_key, audio_url = volcengine_tos.upload_file(Path(args.audio), expires=args.url_expires)
        if args.upload_json_output:
            upload_payload = {"key": object_key, "url": audio_url}
            Path(args.upload_json_output).parent.mkdir(parents=True, exist_ok=True)
            Path(args.upload_json_output).write_text(
                json.dumps(upload_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        response_text = volcengine_asr.call_asr_url(audio_url)
        transcript = volcengine_asr.extract_text(response_text)
        corrected = ""
        correction_report = {}

        if args.json_output:
            Path(args.json_output).parent.mkdir(parents=True, exist_ok=True)
            Path(args.json_output).write_text(response_text, encoding="utf-8")
        if args.text_output:
            Path(args.text_output).parent.mkdir(parents=True, exist_ok=True)
            Path(args.text_output).write_text(transcript, encoding="utf-8")
        if args.corrected_text_output or args.correction_report_output or args.feishu_table_id:
            glossary_path = Path(args.glossary)
            glossary = correct_transcript.load_glossary(glossary_path)
            replacements = correct_transcript.collect_replacements(
                glossary,
                set(args.correction_domain) if args.correction_domain else None,
            )
            corrected, changes = correct_transcript.apply_replacements(transcript, replacements)
            candidates = correct_transcript.extract_candidates(
                corrected,
                correct_transcript.collect_hotwords(glossary),
            )
            if args.corrected_text_output:
                Path(args.corrected_text_output).parent.mkdir(parents=True, exist_ok=True)
                Path(args.corrected_text_output).write_text(corrected, encoding="utf-8")
            correction_report = {
                "glossary": str(glossary_path),
                "changes": changes,
                "candidate_terms": candidates,
            }
            if args.correction_report_output:
                Path(args.correction_report_output).parent.mkdir(parents=True, exist_ok=True)
                Path(args.correction_report_output).write_text(
                    json.dumps(correction_report, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        if args.feishu_table_id or args.feishu_work_id:
            if not args.feishu_table_id or not args.feishu_work_id:
                raise SystemExit("--feishu-table-id and --feishu-work-id must be used together.")
            feishu_args = argparse.Namespace(
                transcript=transcript,
                transcript_file=None,
                corrected_transcript=corrected,
                corrected_transcript_file=None,
                transcript_field=args.feishu_transcript_field,
            )
            cli = feishu_transcript_writer.resolve_lark_cli(args.feishu_lark_cli)
            base_token = feishu_transcript_writer.load_base_token(args.feishu_base_token)
            record_id = feishu_transcript_writer.find_record_by_work_id(
                cli,
                base_token,
                args.feishu_table_id,
                args.feishu_work_id,
                args.feishu_work_id_field,
                "user",
            )
            feishu_transcript_writer.write_transcript(
                cli,
                base_token,
                args.feishu_table_id,
                record_id,
                feishu_transcript_writer.build_patch(feishu_args),
                "user",
                dry_run=args.feishu_dry_run,
            )

        print(transcript)
        return 0
    finally:
        if object_key and not args.keep_object:
            volcengine_tos.delete_object(object_key)


if __name__ == "__main__":
    raise SystemExit(main())
