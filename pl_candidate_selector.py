#!/usr/bin/env python3
import argparse
import json
import os
import re
import math
import pandas as pd
from tqdm import tqdm
import tkinter as tk
from tkinter import filedialog, simpledialog, messagebox

REQUIRED_COLUMNS = [
    "BaseVehicleID", "Category_new", "subcategory_new", "VehicleID", "SubCategoryUrl",
    "samePNCOUNT", "PN_M", "PN_G", "sumcount", "like", "by1"
]


def _normalize_colname(name: str) -> str:
    return re.sub(r"\s+", "", str(name or "")).lower()


def _build_column_aliases(cols):
    alias = {}
    for c in cols:
        alias[_normalize_colname(c)] = c
    return alias


def _ensure_required_columns(df: pd.DataFrame) -> pd.DataFrame:
    alias = _build_column_aliases(df.columns)
    rename_map = {}
    missing = []
    for req in REQUIRED_COLUMNS:
        norm = _normalize_colname(req)
        if req in df.columns:
            continue
        if norm in alias:
            rename_map[alias[norm]] = req
        else:
            missing.append(req)

    # 兼容异常表头：VehicleID 与 SubCategoryUrl 被并成一个列名
    if missing and ("VehicleID" in missing or "SubCategoryUrl" in missing):
        combined_col = None
        for c in df.columns:
            nc = _normalize_colname(c)
            if "vehicleid" in nc and "subcategoryurl" in nc:
                combined_col = c
                break
        if combined_col is not None:
            if "SubCategoryUrl" in missing:
                rename_map[combined_col] = "SubCategoryUrl"
                missing = [m for m in missing if m != "SubCategoryUrl"]
            if "VehicleID" in missing:
                df["VehicleID"] = ""
                missing = [m for m in missing if m != "VehicleID"]

    if rename_map:
        df = df.rename(columns=rename_map)

    if missing:
        fail(f"Input is missing required columns: {missing}")
    return df


def fail(msg: str):
    print(f"ERROR: {msg}")
    try:
        messagebox.showerror("Error", msg)
    except Exception:
        pass
    raise RuntimeError(msg)




def robust_read_csv(path: str, source_label: str) -> pd.DataFrame:
    df = None
    read_errors = []
    attempts = [
        {"encoding": "utf-8-sig", "sep": ",", "engine": "c"},
        {"encoding": "utf-8", "sep": ",", "engine": "c"},
        {"encoding": "gb18030", "sep": ",", "engine": "python"},
        {"encoding": "gbk", "sep": ",", "engine": "python"},
        {"encoding": "utf-8-sig", "sep": None, "engine": "python"},
        {"encoding": "gb18030", "sep": None, "engine": "python"},
    ]
    for opt in attempts:
        try:
            df = pd.read_csv(path, **opt)
            break
        except Exception as e:
            read_errors.append(f"{opt}: {e}")

    if df is None:
        # last fallback: skip malformed lines to avoid tokenizer hard-fail on dirty KB/CSV files
        fallback_attempts = [
            {"encoding": "utf-8-sig", "sep": None, "engine": "python", "on_bad_lines": "skip"},
            {"encoding": "gb18030", "sep": None, "engine": "python", "on_bad_lines": "skip"},
        ]
        for opt in fallback_attempts:
            try:
                df = pd.read_csv(path, **opt)
                print(f"WARNING: {source_label} parsed with on_bad_lines='skip': {path}")
                break
            except Exception as e:
                read_errors.append(f"{opt}: {e}")

    if df is None:
        msg = (
            f"Failed to parse {source_label}: {path}\n"
            "Please ensure proper delimiter/quoting or save as .xlsx. Attempts:\n"
            + "\n".join(read_errors[-4:])
        )
        fail(msg)
    return df
def read_table(path: str) -> pd.DataFrame:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        df = robust_read_csv(path, "input CSV")
    elif ext in {".xlsx", ".xls"}:
        df = pd.read_excel(path)
    else:
        fail(f"Unsupported input file type: {ext}")
    return _ensure_required_columns(df)


def _resolve_manifest_path(kb_root: str, manifest: dict, key: str, default_rel: str) -> str:
    rel = manifest.get(key, default_rel)
    if isinstance(rel, dict):
        rel = rel.get("path", default_rel)
    if not isinstance(rel, str) or not rel.strip():
        rel = default_rel
    p = os.path.join(kb_root, rel)
    if not os.path.exists(p):
        p = os.path.join(kb_root, default_rel)
    if not os.path.exists(p):
        fail(f"KB file not found for {key}: expected '{rel}' or '{default_rel}'")
    return p


def load_kb(kb_root: str):
    manifest_path = os.path.join(kb_root, "kb_manifest.json")
    if not os.path.exists(manifest_path):
        fail(f"kb_manifest.json not found in KB directory: {kb_root}")
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    stopwords_path = _resolve_manifest_path(kb_root, manifest, "url_stopwords", "rules/url_stopwords.csv")
    domain_guard_path = _resolve_manifest_path(kb_root, manifest, "domain_guard", "rules/domain_guard.csv")
    synonyms_path = _resolve_manifest_path(kb_root, manifest, "lexicon_synonyms", "lexicon/lexicon_synonyms.csv")
    concept_links_path = _resolve_manifest_path(kb_root, manifest, "concept_links", "lexicon/concept_links.csv")

    sw_df = robust_read_csv(stopwords_path, "KB url_stopwords.csv")
    dg_df = robust_read_csv(domain_guard_path, "KB domain_guard.csv")
    syn_df = robust_read_csv(synonyms_path, "KB lexicon_synonyms.csv")
    cl_df = robust_read_csv(concept_links_path, "KB concept_links.csv")

    stop_col = "stopwords" if "stopwords" in sw_df.columns else sw_df.columns[0]
    stopwords = {str(x).strip().lower() for x in sw_df[stop_col].dropna().tolist() if str(x).strip()}

    guard = {}
    required_dg = {"new_category", "old_url_domain", "mode"}
    if not required_dg.issubset(set(dg_df.columns)):
        fail("domain_guard.csv must contain columns: new_category, old_url_domain, mode (optional penalty)")
    for _, r in dg_df.iterrows():
        new_cat = str(r.get("new_category", "")).strip().lower()
        old_dom = str(r.get("old_url_domain", "")).strip().lower()
        mode = str(r.get("mode", "allow")).strip().lower()
        penalty = r.get("penalty", 0)
        try:
            penalty = float(penalty)
        except Exception:
            penalty = 0.0
        guard[(new_cat, old_dom)] = (mode, penalty)

    required_syn = {"term", "norm", "concept"}
    if not required_syn.issubset(set(syn_df.columns)):
        fail("lexicon_synonyms.csv must contain columns: term, norm, concept (optional weight)")
    synonym_map = {}
    for _, r in syn_df.iterrows():
        term = str(r.get("term", "")).strip().lower()
        if not term:
            continue
        norm = str(r.get("norm", "")).strip().lower() or term
        concept = str(r.get("concept", "")).strip().lower()
        w = r.get("weight", 1.0)
        try:
            w = float(w)
        except Exception:
            w = 1.0
        old = synonym_map.get(term)
        if old is None or w >= old[2]:
            synonym_map[term] = (norm, concept, w)

    required_cl = {"concept_a", "concept_b", "score"}
    if not required_cl.issubset(set(cl_df.columns)):
        fail("concept_links.csv must contain columns: concept_a, concept_b, score")
    links = {}
    for _, r in cl_df.iterrows():
        a = str(r.get("concept_a", "")).strip().lower()
        b = str(r.get("concept_b", "")).strip().lower()
        if not a or not b:
            continue
        try:
            s = float(r.get("score", 0))
        except Exception:
            s = 0.0
        s = max(0.0, min(1.0, s))
        links[(a, b)] = max(s, links.get((a, b), 0.0))
        links[(b, a)] = max(s, links.get((b, a), 0.0))

    return stopwords, guard, synonym_map, links


def split_tokens(text: str):
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if t]


def parse_old_url(url: str):
    s = str(url).strip().lower()
    if "/" in s:
        i = s.find("/")
        domain = s[:i]
        tail = s.split("/")[-1]
    else:
        domain = ""
        tail = s
    return domain, tail


def norm_and_concepts(tokens, synonym_map):
    norms, concepts = set(), set()
    for t in tokens:
        if t in synonym_map:
            n, c, _ = synonym_map[t]
            norms.add(n)
            if c:
                concepts.add(c)
        else:
            norms.add(t)
    return norms, concepts


def token_overlap(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    u = a | b
    if not u:
        return 0.0
    return len(a & b) / len(u)


def concept_link_score(new_concepts: set, old_concepts: set, links: dict) -> float:
    if not new_concepts or not old_concepts:
        return 0.0
    vals = []
    for nc in new_concepts:
        best = 0.0
        for oc in old_concepts:
            if nc == oc:
                best = max(best, 1.0)
            else:
                best = max(best, links.get((nc, oc), 0.0))
        vals.append(best)
    return sum(vals) / len(vals) if vals else 0.0


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def main():
    parser = argparse.ArgumentParser(description="Score old PL candidates per new PL group and select top-1 by1.")
    parser.parse_args()

    root = tk.Tk()
    root.withdraw()

    input_path = filedialog.askopenfilename(
        title="Select input Excel/CSV file",
        filetypes=[("Data files", "*.xlsx *.xls *.csv"), ("All files", "*.*")]
    )
    if not input_path:
        fail("No input file selected. Operation cancelled.")

    kb_root = filedialog.askdirectory(title="Select KB root directory (contains kb_manifest.json)")
    if not kb_root:
        fail("No KB directory selected. Operation cancelled.")

    in_dir = os.path.dirname(input_path)
    in_name = os.path.basename(input_path)
    stem, ext = os.path.splitext(in_name)
    default_ext = ".xlsx" if ext.lower() in {".xlsx", ".xls"} else ".csv"
    default_out_name = f"{stem}_scored{default_ext}"
    default_output_path = os.path.join(in_dir, default_out_name)

    out_path_input = simpledialog.askstring(
        "Output file path",
        "Enter full output file path (or file name). Must end with .xlsx or .csv:",
        initialvalue=default_output_path
    )
    if out_path_input is None or not out_path_input.strip():
        fail("Output file path is empty/cancelled. Operation cancelled.")
    out_path_input = out_path_input.strip()
    output_path = out_path_input if os.path.isabs(out_path_input) else os.path.join(in_dir, out_path_input)
    output_path = os.path.abspath(output_path)

    confirm_msg = f"Output will be written to:\n{output_path}\n\nContinue?"
    if not messagebox.askokcancel("Confirm output path", confirm_msg):
        fail("User cancelled output path confirmation.")

    try:
        df = read_table(input_path)
        stopwords, guard, synonym_map, links = load_kb(kb_root)

        numeric_cols = ["samePNCOUNT", "PN_M", "PN_G", "sumcount"]
        for c in numeric_cols:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

        group_cols = ["BaseVehicleID", "Category_new", "subcategory_new"]
        grouped = df.groupby(group_cols, dropna=False)
        sum_min = grouped["sumcount"].transform("min")
        sum_max = grouped["sumcount"].transform("max")
        diff = (sum_max - sum_min)
        sum_norm = pd.Series([0.5] * len(df), index=df.index, dtype=float)
        nz = diff != 0
        sum_norm.loc[nz] = (df.loc[nz, "sumcount"] - sum_min.loc[nz]) / diff.loc[nz]

        likes = []
        overlaps_map = {}
        hard_reject_flags = []

        print("Stage 1/2: calculating like for each row...")
        for _, row in tqdm(df.iterrows(), total=len(df), desc="Scoring rows", ncols=100):
            domain, tail = parse_old_url(row["SubCategoryUrl"])

            old_tokens_raw = [t for t in split_tokens(tail) if t not in stopwords]
            new_text = f"{row['Category_new']} {row['subcategory_new']}"
            new_tokens_raw = [t for t in split_tokens(new_text) if t not in stopwords]

            old_norm, old_concepts = norm_and_concepts(old_tokens_raw, synonym_map)
            new_norm, new_concepts = norm_and_concepts(new_tokens_raw, synonym_map)

            tok = token_overlap(new_norm, old_norm)
            cls = concept_link_score(new_concepts, old_concepts, links)
            semantic = 0.7 * tok + 0.3 * cls

            mode, penalty = guard.get((str(row["Category_new"]).strip().lower(), domain), ("allow", 0.0))
            hard_reject = False
            if mode == "deny":
                semantic = 0.0
                hard_reject = True
            elif mode == "penalize":
                # penalty scheme: multiplicative attenuation
                semantic = semantic * (1 - abs(float(penalty)))
            semantic = clamp01(semantic)

            denom = max(float(row["PN_M"]), float(row["PN_G"]), 1.0)
            overlap_rate = min(1.0, float(row["samePNCOUNT"]) / denom)

            sr = float(sum_norm.loc[row.name])
            like_raw = 0.55 * semantic + 0.35 * overlap_rate + 0.10 * sr
            like = int(round(1 + 99 * like_raw))
            like = min(100, max(1, like))

            likes.append(like)
            overlaps_map[row.name] = overlap_rate
            hard_reject_flags.append(hard_reject)

        df["like"] = likes

        print("Stage 2/2: selecting top-1 by1 in each new PL group...")
        group_indices = list(df.groupby(group_cols, dropna=False).groups.values())
        by1_is_numeric = pd.api.types.is_numeric_dtype(df["by1"])
        by1_value = 1 if by1_is_numeric else "1"
        for idxs in tqdm(group_indices, total=len(group_indices), desc="Selecting groups", ncols=100):
            ordered = sorted(
                list(idxs),
                key=lambda i: (
                    -int(df.at[i, "like"]),
                    -float(df.at[i, "samePNCOUNT"]),
                    -float(overlaps_map.get(i, 0.0)),
                    -float(df.at[i, "sumcount"]),
                    str(df.at[i, "SubCategoryUrl"])
                )
            )
            top_i = ordered[0]
            df.at[top_i, "by1"] = by1_value

        out_ext = os.path.splitext(output_path)[1].lower()
        if out_ext == ".csv":
            df.to_csv(output_path, index=False, encoding="utf-8")
        elif out_ext == ".xlsx":
            df.to_excel(output_path, index=False, engine="openpyxl")
        else:
            fail("Output file extension must be .xlsx or .csv")

        total_rows = len(df)
        total_groups = len(group_indices)
        hard_reject_count = int(sum(hard_reject_flags))
        bins = {
            "1-20": int(((df["like"] >= 1) & (df["like"] <= 20)).sum()),
            "21-40": int(((df["like"] >= 21) & (df["like"] <= 40)).sum()),
            "41-60": int(((df["like"] >= 41) & (df["like"] <= 60)).sum()),
            "61-80": int(((df["like"] >= 61) & (df["like"] <= 80)).sum()),
            "81-100": int(((df["like"] >= 81) & (df["like"] <= 100)).sum()),
        }
        print("\n=== SUMMARY ===")
        print(f"Total rows: {total_rows}")
        print(f"New PL groups: {total_groups}")
        print(f"Hard reject rows: {hard_reject_count}")
        print("Like buckets:")
        for k, v in bins.items():
            print(f"  {k}: {v}")

        messagebox.showinfo("Done", f"Processing completed.\nOutput: {output_path}")
    except Exception as e:
        fail(str(e))


if __name__ == "__main__":
    main()
