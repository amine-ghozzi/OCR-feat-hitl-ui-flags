"""Read-only workflow presentation shared by local and API processing."""

STAGES = [
    ("identification", "Identification et qualité de lecture"),
    ("admissibility", "Admissibilité du document"),
    ("extraction", "Extraction, mapping, scores et règles"),
    ("framing", "Cadrage des états et des unités"),
    ("review", "Comparaison et validation humaine"),
]


def render_progress(st, events):
    latest = {e.get("stage"): e for e in events}
    icons = {"completed": "✓", "running": "◉", "rejected": "✕", "waiting": "○"}
    with st.container(border=True):
        st.markdown("#### Parcours du document")
        for key, label in STAGES:
            item = latest.get(key, {})
            state = item.get("state", "waiting")
            st.markdown(f"{icons.get(state, '○')} **{label}**")
            if item:
                st.caption(item.get("message", ""))
        if events:
            st.caption("Dernier événement : " + events[-1].get("at", ""))
            with st.expander("Journal du traitement"):
                for e in events:
                    st.text(f"{e.get('at', '')} · {e.get('message', '')}")


def render_raw_snapshot(st, raw):
    """Separate source inventory: never align engine tables to ensemble indices."""
    import pandas as pd
    if not raw:
        st.info("Lecture brute indisponible pour cet ancien résultat. Relancez l’analyse pour la conserver.")
        return
    st.caption(f"Moteur : {raw.get('engine', 'inconnu')} · SHA-256 : {raw.get('sha256', '')}")
    st.text_area("Texte avant correction", raw.get("text", ""), height=180, disabled=True)
    parts = [raw] + (raw.get("parts") or [])
    for part_index, part in enumerate(parts):
        for i, table in enumerate(part.get("tables") or []):
            st.caption(f"Lecture source {part_index + 1} · tableau {i + 1}")
            # Display duplicate headers as a first row; pandas/Arrow cannot expose
            # duplicate column labels, whereas the stored snapshot preserves them.
            rows = [table.get("columns", [])] + table.get("data", [])
            st.dataframe(pd.DataFrame(rows).astype(str), hide_index=True, use_container_width=True)
