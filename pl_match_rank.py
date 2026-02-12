import argparse
import json
import math
import os
import re
from tkinter import Tk, filedialog

import pandas as pd


REQUIRED_COLUMNS = [
    "BaseVehicleID",
    "Category_new",
    "subcategory_new",
    "VehicleID",
    "SubCategoryUrl",
    "samePNCOUNT",
    "PN_M",
    "PN_G",
    "sumcount",
    "like",
    "by1",
]


def _to_float(value, default=0.0):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return default
    try:
        return float(value)
    except Exception:
        return default


def _normalize_text(value):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return str(value)


def _tokenize_text(text, pattern):
    parts = re.split(pattern, text.lower())
    return [p for p in parts if p]


def _flatten_manifest_strings(obj, out):
    if isinstance(obj, dict):
        for v in obj.values():
            _flatten_manifest_strings(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _flatten_manifest_strings(v, out)
    elif isinstance(obj, str):
        out.add(obj.replace("\\", "/"))


def _resolve_manifest_csv(kb_root, manifest, rel_path):
    rel_norm = rel_path.replace("\\", "/")
    paths = set()
    _flatten_manifest_strings(manifest, paths)
    if rel_norm not in paths:
        raise FileNotFoundError(f"KB manifest missing required path: {rel_norm}")
    full = os.path.join(kb_root, rel_path)
    if not os.path.isfile(full):
        raise FileNotFoundError(f"KB file not found: {full}")
    return full


def load_kb(kb_root):
    manifest_path = os.path.join(kb_root, "kb_manifest.json")
    if not os.path.isfile(manifest_path):
        raise FileNotFoundError(f"KB manifest not found: {manifest_path}")
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    stopwords_path = _resolve_manifest_csv(kb_root, manifest, "rules/url_stopwords.csv")
    guard_path = _resolve_manifest_csv(kb_root, manifest, "rules/domain_guard.csv")
    syn_path = _resolve_manifest_csv(kb_root, manifest, "lexicon/lexicon_synonyms.csv")
    link_path = _resolve_manifest_csv(kb_root, manifest, "lexicon/concept_links.csv")

    stop_df = pd.read_csv(stopwords_path)
    if "stopwords" not in stop_df.columns:
        raise ValueError("rules/url_stopwords.csv missing required column: stopwords")
    stopwords = set(
        str(x).strip().lower()
        for x in stop_df["stopwords"].dropna().tolist()
        if str(x).strip()
    )

    guard_df = pd.read_csv(guard_path)
    needed_guard = {"new_category", "old_url_domain", "mode"}
    if not needed_guard.issubset(set(guard_df.columns)):
        raise ValueError(
            "rules/domain_guard.csv missing required columns: new_category, old_url_domain, mode"
        )
    guard_map = {}
    for _, row in guard_df.iterrows():
        new_cat = str(row["new_category"]).strip().lower()
        old_domain = str(row["old_url_domain"]).strip().lower()
        mode = str(row["mode"]).strip().lower()
        penalty = _to_float(row["penalty"], 0.0) if "penalty" in guard_df.columns else 0.0
        guard_map[(new_cat, old_domain)] = (mode, penalty)

    syn_df = pd.read_csv(syn_path)
    needed_syn = {"term", "norm", "concept", "weight"}
    if not needed_syn.issubset(set(syn_df.columns)):
        raise ValueError(
            "lexicon/lexicon_synonyms.csv missing required columns: term, norm, concept, weight"
        )
    syn_map = {}
    for _, row in syn_df.iterrows():
        term = str(row["term"]).strip().lower()
        if not term:
            continue
        norm = str(row["norm"]).strip().lower()
        concept = str(row["concept"]).strip().lower()
        if concept == "nan":
            concept = ""
        weight = _to_float(row["weight"], 0.0)
        prev = syn_map.get(term)
        if prev is None or weight > prev[2]:
            syn_map[term] = (norm if norm else term, concept, weight)

    link_df = pd.read_csv(link_path)
    needed_link = {"concept_a", "concept_b", "score"}
    if not needed_link.issubset(set(link_df.columns)):
        raise ValueError(
            "lexicon/concept_links.csv missing required columns: concept_a, concept_b, score"
        )
    link_map = {}
    for _, row in link_df.iterrows():
        a = str(row["concept_a"]).strip().lower()
        b = str(row["concept_b"]).strip().lower()
        if not a or not b:
            continue
        score = _to_float(row["score"], 0.0)
        score = max(0.0, min(1.0, score))
        if (a, b) not in link_map or score > link_map[(a, b)]:
            link_map[(a, b)] = score
        if (b, a) not in link_map or score > link_map[(b, a)]:
            link_map[(b, a)] = score

    return stopwords, guard_map, syn_map, link_map


def normalize_tokens(tokens, syn_map):
    norm_tokens = set()
    concepts = set()
    for tok in tokens:
        if tok in syn_map:
            norm, concept, _ = syn_map[tok]
        else:
            norm, concept = tok, ""
        if norm:
            norm_tokens.add(norm)
        if concept:
            concepts.add(concept)
    return norm_tokens, concepts


def token_overlap_score(a_set, b_set):
    union = a_set | b_set
    if not union:
        return 0.0
    return len(a_set & b_set) / len(union)


def concept_link_score(new_concepts, old_concepts, link_map):
    if not new_concepts or not old_concepts:
        return 0.0
    best_scores = []
    for nc in sorted(new_concepts):
        best = 0.0
        for oc in old_concepts:
            s = link_map.get((nc, oc), 0.0)
            if s > best:
                best = s
        best_scores.append(best)
    if not best_scores:
        return 0.0
    return sum(best_scores) / len(best_scores)


def parse_old_url(url):
    raw = _normalize_text(url).strip()
    if not raw:
        return "", []
    parts = [p for p in raw.split("/") if p != ""]
    if not parts:
        return "", []
    old_domain = parts[0].lower()
    url_tail = parts[-1]
    tokens = _tokenize_text(url_tail, r"[_\-]+|[^a-zA-Z0-9]+")
    return old_domain, tokens


def parse_new_text(category_new, subcategory_new):
    text = f"{_normalize_text(category_new)} {_normalize_text(subcategory_new)}"
    return _tokenize_text(text, r"[\s&\-/]+|[^a-zA-Z0-9]+")


def select_input_file():
    path = ""
    try:
        root = Tk()
        root.withdraw()
        root.update()
        path = filedialog.askopenfilename(
            title="Select input file",
            filetypes=[
                ("Data files", "*.xlsx *.xls *.csv"),
                ("Excel files", "*.xlsx *.xls"),
                ("CSV files", "*.csv"),
                ("All files", "*.*"),
            ],
        )
        root.destroy()
    except Exception:
        path = input("Please input full path of input file (.xlsx/.xls/.csv): ").strip()
    if not path:
        raise ValueError("No input file selected.")
    return path


def select_kb_folder():
    path = ""
    try:
        root = Tk()
        root.withdraw()
        root.update()
        path = filedialog.askdirectory(title="Select KB root folder")
        root.destroy()
    except Exception:
        path = input("Please input full path of KB root folder: ").strip()
    if not path:
        raise ValueError("No KB folder selected.")
    return path


def main():
    parser = argparse.ArgumentParser(description="PL match ranking and by1 selection")
    parser.add_argument("--input", required=False, default=None, help="Input Excel/CSV path")
    parser.add_argument("--kb", required=False, default=None, help="KB root folder")
    parser.add_argument("--sheet", default=None, help="Sheet name for Excel input")
    parser.add_argument("--topk", type=int, default=1, help="Top-k selection per new PL group")
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Manually select input file and KB folder via dialog",
    )
    args = parser.parse_args()

    if args.topk != 1:
        raise ValueError("Only --topk 1 is supported in this version.")

    if args.interactive:
        input_path = select_input_file()
        kb_root = select_kb_folder()
    else:
        input_path = args.input
        kb_root = args.kb
        if not input_path:
            raise ValueError("--input is required unless --interactive is used.")
        if not kb_root:
            raise ValueError("--kb is required unless --interactive is used.")

    ext = os.path.splitext(input_path)[1].lower()
    if ext in [".xlsx", ".xls"]:
        df = pd.read_excel(input_path, sheet_name=args.sheet, engine="openpyxl")
    elif ext == ".csv":
        df = pd.read_csv(input_path, encoding="utf-8")
    else:
        raise ValueError("Unsupported input file type. Use .xlsx/.xls/.csv")

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Input file missing required columns: {', '.join(missing)}")

    original_columns = df.columns.tolist()
    stopwords, guard_map, syn_map, link_map = load_kb(kb_root)

    overlap_rates = {}
    semantics = {}
    hard_reject_flags = {}

    for idx, row in df.iterrows():
        old_domain, old_tokens_raw = parse_old_url(row["SubCategoryUrl"])
        old_tokens = [t for t in old_tokens_raw if t not in stopwords]

        new_tokens_raw = parse_new_text(row["Category_new"], row["subcategory_new"])
        new_tokens = [t for t in new_tokens_raw if t not in stopwords]

        new_norm, new_concepts = normalize_tokens(new_tokens, syn_map)
        old_norm, old_concepts = normalize_tokens(old_tokens, syn_map)

        tok_score = token_overlap_score(new_norm, old_norm)
        c_score = concept_link_score(new_concepts, old_concepts, link_map)
        semantic = 0.7 * tok_score + 0.3 * c_score

        new_cat_key = _normalize_text(row["Category_new"]).strip().lower()
        mode_pen = guard_map.get((new_cat_key, old_domain), None)
        hard_reject = False
        if mode_pen is not None:
            mode, penalty = mode_pen
            if mode == "deny":
                semantic = 0.0
                hard_reject = True
            elif mode == "penalize":
                semantic = max(0.0, semantic - abs(penalty))
            elif mode == "allow":
                pass

        same_pn = _to_float(row["samePNCOUNT"], 0.0)
        pn_m = _to_float(row["PN_M"], 0.0)
        pn_g = _to_float(row["PN_G"], 0.0)
        denom = max(pn_m, pn_g, 1.0)
        overlap_rate = min(1.0, max(0.0, same_pn / denom))

        semantics[idx] = max(0.0, min(1.0, semantic))
        overlap_rates[idx] = overlap_rate
        hard_reject_flags[idx] = hard_reject

    likes = pd.Series(index=df.index, dtype="int64")
    group_cols = ["Category_new", "subcategory_new"]
    for _, gidx in df.groupby(group_cols, sort=False).groups.items():
        g_rows = df.loc[gidx]
        sum_vals = [_to_float(v, 0.0) for v in g_rows["sumcount"].tolist()]
        mn = min(sum_vals) if sum_vals else 0.0
        mx = max(sum_vals) if sum_vals else 0.0
        for ridx in gidx:
            s = _to_float(df.at[ridx, "sumcount"], 0.0)
            if mx > mn:
                sum_norm = (s - mn) / (mx - mn)
            else:
                sum_norm = 0.5
            sum_norm = max(0.0, min(1.0, sum_norm))
            like_raw = 0.55 * semantics[ridx] + 0.35 * overlap_rates[ridx] + 0.10 * sum_norm
            like = int(round(1 + 99 * like_raw))
            like = max(1, min(100, like))
            likes.at[ridx] = like

    df["like"] = likes

    for _, gidx in df.groupby(group_cols, sort=False).groups.items():
        ordered = sorted(
            list(gidx),
            key=lambda i: (
                -int(df.at[i, "like"]),
                -_to_float(df.at[i, "samePNCOUNT"], 0.0),
                -overlap_rates[i],
                -_to_float(df.at[i, "sumcount"], 0.0),
                str(df.at[i, "SubCategoryUrl"]),
            ),
        )
        if ordered:
            df.at[ordered[0], "by1"] = "1"

    df = df[original_columns]

    out_path = f"{os.path.splitext(input_path)[0]}_scored{ext}"
    if ext in [".xlsx", ".xls"]:
        df.to_excel(out_path, index=False, engine="openpyxl")
    else:
        df.to_csv(out_path, index=False, encoding="utf-8")

    total_rows = len(df)
    group_count = df.groupby(group_cols, sort=False).ngroups
    hard_reject_count = sum(1 for v in hard_reject_flags.values() if v)
    buckets = {
        "1-20": int(((df["like"] >= 1) & (df["like"] <= 20)).sum()),
        "21-40": int(((df["like"] >= 21) & (df["like"] <= 40)).sum()),
        "41-60": int(((df["like"] >= 41) & (df["like"] <= 60)).sum()),
        "61-80": int(((df["like"] >= 61) & (df["like"] <= 80)).sum()),
        "81-100": int(((df["like"] >= 81) & (df["like"] <= 100)).sum()),
    }

    print(f"total_rows: {total_rows}")
    print(f"new_pl_groups: {group_count}")
    print(f"hard_reject_rows: {hard_reject_count}")
    print(
        "like_buckets: "
        + ", ".join([f"{k}={v}" for k, v in buckets.items()])
    )


if __name__ == "__main__":
    main()
