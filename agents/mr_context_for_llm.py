# -*- coding: utf-8 -*-
"""
SMAR Tool: 根据用户输入的 GitLab MR 链接，拉取 MR 的完整上下文并格式化为适合 LLM 使用的文本。

依赖库（需在 SMAR Step 1 > Dependency Library 中勾选）:
- requests

入口: main(input) -> dict
- input: Step 2 配置的输入参数字典，需包含 mr_url；可选 gitlab_private_token（私有仓库必填）。
- return: 与 Step 2 配置的输出参数一致的字典，包含 mr_context 等。
"""

import re
import requests


def parse_mr_url(mr_url: str) -> tuple:
    """
    从 GitLab MR 链接解析出 base_url, project_id(路径形式), merge_request_iid。
    支持格式:
    - https://gitlab.xxx/group/project/-/merge_requests/123
    - https://gitlab.xxx/group/project/merge_requests/123
    """
    mr_url = mr_url.strip().rstrip("/")
    # 匹配 /merge_requests/ 或 /-/merge_requests/ 后的数字
    pattern = r"^(https?://[^/]+)/(.+?)(?:/-)?/merge_requests/(\d+)$"
    m = re.match(pattern, mr_url, re.IGNORECASE)
    if not m:
        return None, None, None
    base_url = m.group(1).rstrip("/")
    project_path = m.group(2).strip("/")
    mr_iid = int(m.group(3))
    # GitLab API 中 project_id 可以是 URL 编码的 path，如 group%2Fproject
    project_id = project_path.replace("/", "%2F")
    return base_url, project_id, mr_iid


def _auth_headers(token: str = None, use_oauth: bool = False):
    """
    GitLab API 认证：
    - Personal Access Token (PAT)：用 PRIVATE-TOKEN 头，token 在 GitLab 设置里创建。
    - OAuth 2.0 access_token：用 Authorization: Bearer，token 来自 OAuth 授权流程。
    二者不同，不能混用；本脚本默认按 PAT，若传的是 OAuth token 请设 use_oauth=True 或环境变量 GITLAB_USE_OAUTH_TOKEN=1。
    """
    if not token:
        return {}
    import os
    if use_oauth or (os.environ.get("GITLAB_USE_OAUTH_TOKEN", "").strip() in ("1", "true", "yes")):
        return {"Authorization": f"Bearer {token}"}
    return {"PRIVATE-TOKEN": token}


def fetch_mr_details(base_url: str, project_id: str, mr_iid: int, token: str = None, use_oauth: bool = False) -> dict:
    """获取 MR 基本信息。"""
    url = f"{base_url}/api/v4/projects/{project_id}/merge_requests/{mr_iid}"
    headers = _auth_headers(token, use_oauth)
    r = requests.get(url, headers=headers or None, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_mr_changes(base_url: str, project_id: str, mr_iid: int, token: str = None, use_oauth: bool = False) -> list:
    """获取 MR 的 changes（含 diff）。"""
    url = f"{base_url}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/changes"
    headers = _auth_headers(token, use_oauth)
    r = requests.get(url, headers=headers or None, timeout=60)
    r.raise_for_status()
    data = r.json()
    return data.get("changes", [])


def build_llm_context(mr_detail: dict, changes: list, max_diff_lines: int = 2000) -> str:
    """
    将 MR 详情与 changes 组装成一段适合喂给 LLM 的上下文文本。
    diff 过长时做截断并注明行数。
    """
    lines = []

    lines.append("## Merge Request 基本信息")
    lines.append(f"- 标题: {mr_detail.get('title', '')}")
    lines.append(f"- 描述:\n{mr_detail.get('description') or '(无)'}")
    lines.append(f"- 源分支: {mr_detail.get('source_branch', '')} -> 目标分支: {mr_detail.get('target_branch', '')}")
    lines.append(f"- 作者: {mr_detail.get('author', {}).get('name', '')} ({mr_detail.get('author', {}).get('username', '')})")
    lines.append(f"- 状态: {mr_detail.get('state', '')}")
    lines.append("")

    lines.append("## 变更文件与 Diff")
    total_diff_lines = 0
    for ch in changes:
        old_path = ch.get("old_path") or "(新文件)"
        new_path = ch.get("new_path", old_path)
        lines.append(f"### {new_path}")
        diff = ch.get("diff", "")
        if diff:
            diff_lines = diff.splitlines()
            total_diff_lines += len(diff_lines)
            if total_diff_lines <= max_diff_lines:
                lines.append("```")
                lines.append(diff)
                lines.append("```")
            else:
                remaining = max_diff_lines - (total_diff_lines - len(diff_lines))
                if remaining > 0:
                    lines.append("```")
                    lines.append("\n".join(diff_lines[:remaining]))
                    lines.append("```")
                    lines.append(f"... (diff 已截断，本文件共 {len(diff_lines)} 行)")
                else:
                    lines.append(f"(diff 已省略，共 {len(diff_lines)} 行)")
        lines.append("")

    if total_diff_lines > max_diff_lines:
        lines.append(f"[说明: 为控制长度，仅保留前 {max_diff_lines} 行 diff，总变更行数约 {total_diff_lines}]")

    return "\n".join(lines).strip()


def main(input: dict) -> dict:
    """
    SMAR Tool 入口。
    输入参数（Step 2 配置）:
    - mr_url (str, 必填): GitLab MR 完整链接，如 https://gitlab.xxx/group/project/-/merge_requests/123
    - gitlab_private_token (str, 可选): 私有仓库或需更高权限时填写。支持两种：① Personal Access Token (PAT)，在 GitLab 设置里创建；② OAuth 2.0 的 access_token，此时需同时传 use_oauth=True 或设环境变量 GITLAB_USE_OAUTH_TOKEN=1
    - max_diff_lines (int, 可选): 向 LLM 提供的 diff 最大行数，默认 2000，避免超长
    输出参数（Step 2 配置）:
    - mr_context (str): 格式化后的 MR 上下文，可直接作为 LLM 的上下文输入
    - title (str): MR 标题
    - changed_files_count (int): 变更文件数
    - error_message (str): 若出错则在此返回错误信息，成功时为空字符串
    """
    mr_url = (input.get("mr_url") or "").strip()
    gitlab_token = (input.get("gitlab_private_token") or "").strip() or None
    use_oauth = input.get("use_oauth") in (True, 1, "1", "true", "yes")
    max_diff_lines = int(input.get("max_diff_lines") or 2000)

    if not mr_url:
        return {
            "mr_context": "",
            "title": "",
            "changed_files_count": 0,
            "error_message": "请输入 MR 链接 (mr_url)。",
        }

    base_url, project_id, mr_iid = parse_mr_url(mr_url)
    if not base_url or not project_id or not mr_iid:
        return {
            "mr_context": "",
            "title": "",
            "changed_files_count": 0,
            "error_message": f"无法解析 MR 链接，请确认格式类似: https://gitlab.xxx/group/project/-/merge_requests/123，当前输入: {mr_url[:200]}",
        }

    try:
        mr_detail = fetch_mr_details(base_url, project_id, mr_iid, gitlab_token, use_oauth)
        changes = fetch_mr_changes(base_url, project_id, mr_iid, gitlab_token, use_oauth)
        context_text = build_llm_context(mr_detail, changes, max_diff_lines)
        return {
            "mr_context": context_text,
            "title": mr_detail.get("title", ""),
            "changed_files_count": len(changes),
            "error_message": "",
        }
    except requests.HTTPError as e:
        err_msg = f"GitLab API 请求失败: {e.response.status_code} - {e.response.text[:500] if e.response.text else str(e)}"
        return {
            "mr_context": "",
            "title": "",
            "changed_files_count": 0,
            "error_message": err_msg,
        }
    except Exception as e:
        return {
            "mr_context": "",
            "title": "",
            "changed_files_count": 0,
            "error_message": f"处理 MR 时出错: {str(e)}",
        }


if __name__ == "__main__":
    """本地运行：python -m agents.mr_context_for_llm <MR链接> [--token xxx] [--out file.txt]"""
    import os
    import argparse
    parser = argparse.ArgumentParser(description="拉取 GitLab MR 上下文并格式化为 LLM 可读文本")
    parser.add_argument("mr_url", nargs="?", default="", help="GitLab MR 完整链接，如 https://gitlab.xxx/group/project/-/merge_requests/123")
    parser.add_argument("--token", "-t", default="", help="GitLab PAT 或 OAuth access_token；也可设环境变量 GITLAB_PRIVATE_TOKEN")
    parser.add_argument("--oauth", action="store_true", help="当前 token 为 OAuth 的 access_token 时加此参数；或设 GITLAB_USE_OAUTH_TOKEN=1")
    parser.add_argument("--out", "-o", default="", help="将 mr_context 写入该文件；不传则打印到 stdout")
    parser.add_argument("--max-diff-lines", type=int, default=2000, help="diff 最大行数，默认 2000")
    args = parser.parse_args()
    mr_url = (args.mr_url or os.environ.get("MR_URL", "")).strip()
    token = (args.token or os.environ.get("GITLAB_PRIVATE_TOKEN", "")).strip() or None
    if not mr_url:
        parser.error("请提供 MR 链接，例如: python -m agents.mr_context_for_llm 'https://gitlab.xxx/group/project/-/merge_requests/123'")
    inp = {"mr_url": mr_url, "gitlab_private_token": token, "use_oauth": args.oauth, "max_diff_lines": args.max_diff_lines}
    out = main(inp)
    if out.get("error_message"):
        print("错误:", out["error_message"], flush=True)
        exit(1)
    ctx = out.get("mr_context", "")
    print(f"标题: {out.get('title', '')} | 变更文件数: {out.get('changed_files_count', 0)}", flush=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(ctx)
        print(f"已写入: {args.out}", flush=True)
    else:
        print("--- mr_context ---", flush=True)
        print(ctx, flush=True)
