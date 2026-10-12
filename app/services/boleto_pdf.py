"""Render a persisted boleto as a readable PDF.

Used by ``GET /api/v1/groups/{group_id}/porras/{porra_id}/boleto.pdf``.

The output is intentionally structured for human readability, not for
pixel-perfect layout. The previous version used Helvetica (Latin-1
only) which mangled accented team names (e.g. ``"Atlético"`` → ``"Atl?tico"``)
and laid N columns side-by-side in cramped cells. This version:

- Uses **DejaVu Sans** (a TTF bundled with most Linux systems) so UTF-8
  strings render correctly — ``"Atlético de Madrid"`` stays as written.
- For **1 column** (multiple / reducida): one full-width table with
  columns ``# | Local | Visitante | Signo(s)``.
- For **N columns** (intuicion / sencilla): one table PER column, stacked
  vertically. Each table has a "Columna N" header. This is much easier
  to scan than N columns side-by-side.
- Renders the Pleno al 15 in a **highlighted box** so it stands out
  from the 14 partidos.
- Footer: cost + total columns + timestamp + porra ID.

A4 portrait. The function does NOT enforce the column count — callers
can pass any number (1..N). The layout adapts.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from fpdf import FPDF


# DejaVu Sans is the standard Unicode TTF on Debian/Ubuntu/Fedora. The
# function falls back to a matplotlib-bundled copy or the bundled helvetica
# (Latin-1 only — accents become ``?``) if the TTF isn't found.
_DEJAVU_PATHS: list[Path] = [
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/home/davdl/.local/lib/python3.12/site-packages/matplotlib/mpl-data/fonts/ttf/DejaVuSans.ttf"),
]
_DEJAVU_BOLD_PATHS: list[Path] = [
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    Path("/home/davdl/.local/lib/python3.12/site-packages/matplotlib/mpl-data/fonts/ttf/DejaVuSans-Bold.ttf"),
]


def _first_existing(paths: list[Path]) -> Path | None:
    for p in paths:
        if p.exists():
            return p
    return None


class BoletoPDF(FPDF):
    """Custom FPDF with DejaVu Sans registered and a few helpers."""

    def __init__(self) -> None:
        super().__init__(orientation="P", unit="mm", format="A4")
        # Defer add_page to render() so we can pick portrait vs landscape.
        # Tight margins so we have more room for the table body.
        self.set_margins(left=10, top=10, right=10)
        self.set_auto_page_break(auto=True, margin=10)

        regular = _first_existing(_DEJAVU_PATHS)
        bold = _first_existing(_DEJAVU_BOLD_PATHS)
        if regular and bold:
            self.add_font("DejaVu", "", str(regular), uni=True)
            self.add_font("DejaVu", "B", str(bold), uni=True)
            self._font_family = "DejaVu"
        else:
            # Fallback: Helvetica (Latin-1, accents → ``?``). Better than
            # crashing. Logged via stderr so the dev sees it.
            print(
                "WARNING: DejaVu Sans not found at any of the expected "
                "paths. Falling back to Helvetica (Latin-1, accents will "
                "be replaced with '?').",
                flush=True,
            )
            self._font_family = "Helvetica"

    # ---- Layout helpers ----

    def _font(self, style: str = "", size: float = 11) -> None:
        self.set_font(self._font_family, style, size)

    def _heading(self, text: str, size: float = 18) -> None:
        self._font("B", size)
        self.cell(0, 10, text, new_x="LMARGIN", new_y="NEXT")
        self.ln(2)

    def _muted(self, text: str) -> None:
        self._font("", 10)
        self.cell(0, 5, text, new_x="LMARGIN", new_y="NEXT")

    def _divider(self) -> None:
        y = self.get_y()
        self.set_draw_color(180, 180, 180)
        self.line(10, y, 200, y)
        self.ln(2)

    def _table(
        self,
        headers: list[str],
        rows: list[list[str]],
        col_widths: list[float] | None = None,
        zebra: bool = True,
        row_height: float = 7,
        font_size: float = 10,
    ) -> None:
        """Render a clean, zebra-striped table with bordered cells."""
        n_cols = len(headers)
        if col_widths is None:
            available = 190  # A4 width minus 20mm margins
            col_widths = [available / n_cols] * n_cols
        # Sanity: widths must sum to <= 190
        if sum(col_widths) > 190:
            scale = 190 / sum(col_widths)
            col_widths = [w * scale for w in col_widths]

        # Header row (light grey background, bold text).
        self.set_fill_color(220, 220, 220)
        self._font("B", font_size)
        for i, h in enumerate(headers):
            self.cell(col_widths[i], row_height, h, border=1, fill=True,
                      align=("CENTER" if i == 0 or i == n_cols - 1 else "LEFT"),
                      new_x=("RIGHT" if i < n_cols - 1 else "LMARGIN"),
                      new_y="TOP")
        self.ln(row_height)

        # Body rows.
        self._font("", font_size)
        for r_idx, row in enumerate(rows):
            fill = zebra and (r_idx % 2 == 0)
            if fill:
                self.set_fill_color(245, 245, 245)
            else:
                self.set_fill_color(255, 255, 255)
            for i, cell in enumerate(row):
                self.cell(col_widths[i], row_height, str(cell), border=1,
                          fill=fill,
                          align=("CENTER" if i == 0 or i == n_cols - 1 else "LEFT"),
                          new_x=("RIGHT" if i < n_cols - 1 else "LMARGIN"),
                          new_y="TOP")
            self.ln(row_height)
        self.ln(2)


def _signs_str(signs: list[str]) -> str:
    """Render a list of signs as ``1/X/2`` (joined with slashes)."""
    return "/".join(signs) if signs else "—"


def _truncate(text: str, max_chars: int) -> str:
    """Truncate long team names to keep cells readable."""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def render_boleto_pdf(
    *,
    group_name: str,
    jornada_numero: int,
    temporada_str: str,
    columnas: list[list[dict]],
    pleno: tuple[str, str],
    columns_count: int,
    cost_eur: float,
    precio_apuesta_eur: float,
    porra_id: int,
) -> bytes:
    """Render the boleto as a PDF and return the raw bytes.

    Parameters
    ----------
    group_name, jornada_numero, temporada_str
        Header context.
    columnas
        N columns of 14 rows each. Each row is a dict with
        ``partido_orden`` (int), ``local_raw`` (str),
        ``visitante_raw`` (str), ``signs`` (list[str]). Outer list
        ordered (column 0 first).
    pleno
        ``(local, visitante)`` strings in ``{"0","1","2","M"}``.
    columns_count, cost_eur, precio_apuesta_eur, porra_id
        Footer fields.
    """
    pdf = BoletoPDF()

    # Add the first page with the right orientation.
    # - 1 column → portrait, full-width table fits comfortably.
    # - N>1 columns → landscape, more horizontal room for multiple tables
    #   stacked vertically.
    if columns_count > 1:
        pdf.add_page(orientation="L")
    else:
        pdf.add_page(orientation="P")

    # --- Header block.
    pdf._heading("LA QUINIELA — Boleto oficial", size=18)
    pdf._muted(f"Grupo: {group_name}")
    pdf._muted(f"Jornada: {jornada_numero}   Temporada: {temporada_str}")
    pdf._muted(f"Modo: {columns_count} columna{'s' if columns_count != 1 else ''} "
               f"× 0.75€/col = {cost_eur:.2f}€")
    pdf._divider()

    # --- Body: layout strategy depends on column count.
    if not columnas:
        pdf._font("I", 11)
        pdf.cell(0, 7, "(no columns to render)", new_x="LMARGIN", new_y="NEXT")
    else:
        # For 1 column: portrait, full-width table, comfortable row height.
        # For N>1 columns: landscape, 2 tables side-by-side per page-row
        # (with a trailing solo column on the last row if N is odd).
        if columns_count == 1:
            # Portrait: 190mm printable width.
            col_widths = [10, 70, 70, 40]  # sum 190mm
            row_height = 7
            font_size = 10
            sub_header = False  # 1 column — no need for "Columna 1" header

            headers = ["#", "Local", "Visitante", "Signo(s)"]
            rows: list[list[str]] = []
            for m in columnas[0]:
                rows.append([
                    str(m["partido_orden"]),
                    _truncate(str(m.get("local_raw", "?")), 30),
                    _truncate(str(m.get("visitante_raw", "?")), 30),
                    _signs_str(m.get("signs", [])),
                ])
            pdf._table(headers, rows, col_widths=col_widths, zebra=True,
                        row_height=row_height, font_size=font_size)
        else:
            # Landscape: 277mm printable width. 2 tables side-by-side =
            # ~135mm each. Smaller row height for compactness.
            table_width = 130  # mm per table
            col_widths = [10, 50, 50, 20]  # sum 130mm
            row_height = 5
            font_size = 9
            sub_header = True

            # 2 tables per page-row.
            for row_start in range(0, len(columnas), 2):
                row_cols = columnas[row_start:row_start + 2]
                n_in_row = len(row_cols)

                # Render the headers row (vertical alignment between tables).
                for i, col in enumerate(row_cols):
                    if sub_header:
                        pdf._font("B", 11)
                        pdf.cell(
                            table_width, 5,
                            f"Columna {row_start + i + 1}",
                            border=0, align="CENTER",
                            new_x=("RIGHT" if i < n_in_row - 1 else "LMARGIN"),
                            new_y="TOP",
                        )
                pdf.ln(6)

                # Each table: 14 rows.
                for partido_orden in range(1, 15):
                    for i, col in enumerate(row_cols):
                        m = col[partido_orden - 1]
                        headers = ["#", "Local", "Visitante", "Signo(s)"]
                        row = [
                            str(m["partido_orden"]),
                            _truncate(str(m.get("local_raw", "?")), 22),
                            _truncate(str(m.get("visitante_raw", "?")), 22),
                            _signs_str(m.get("signs", [])),
                        ]
                        # Render this cell with zebra fill
                        fill = (partido_orden % 2 == 1)
                        if fill:
                            pdf.set_fill_color(245, 245, 245)
                        else:
                            pdf.set_fill_color(255, 255, 255)
                        pdf._font("", font_size)
                        for j, cell_text in enumerate(row):
                            pdf.cell(
                                col_widths[j], row_height, str(cell_text),
                                border=1, fill=fill,
                                align=("CENTER" if j == 0 or j == 3 else "LEFT"),
                                new_x=("RIGHT" if j < 3 else "LMARGIN"),
                                new_y="TOP",
                            )
                        pdf.ln(row_height)
                pdf.ln(2)

    # --- Pleno al 15 — highlighted box.
    pdf._divider()
    pdf._font("B", 12)
    pdf.cell(0, 6, "Pleno al 15", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)
    pdf.set_fill_color(255, 245, 200)
    pdf._font("B", 16)
    pdf.cell(190, 12,
             f"   {pleno[0]}  —  {pleno[1]}   ",
             border=1, fill=True, align="CENTER",
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # --- Footer.
    pdf._divider()
    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    pdf._muted(
        f"Coste total: {cost_eur:.2f} EUR   ·   "
        f"{columns_count} columna{'s' if columns_count != 1 else ''}   ·   "
        f"Precio/columna: {precio_apuesta_eur:.2f} EUR"
    )
    pdf._muted(
        f"Generado: {now}   ·   Porra ID: {porra_id}"
    )

    return bytes(pdf.output())
