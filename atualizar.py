#!/usr/bin/env python3
"""
Script de atualização automática dos dados de Repetitivos e IACs do STJ.
Extrai dados de https://processo.stj.jus.br/repetitivos/temas_repetitivos/
"""
import urllib.request, ssl, re, json, os, sys, time

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
    'Accept': 'text/html',
    'Accept-Language': 'pt-BR,pt;q=0.9',
}

def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    resp = urllib.request.urlopen(req, context=ctx, timeout=60)
    return resp.read().decode('utf-8', errors='replace')

def parse_page(html):
    themes = []
    # Split by containerDocumento blocks
    blocks = html.split('containerDocumento')

    for block in blocks[1:]:  # skip first (before any container)
        t = {}

        # Tema number
        m = re.search(r'dados_campo_processo\s+fonte_destaque[^>]*>\s*(\d+)', block)
        if not m:
            continue
        t['tema'] = m.group(1)

        # Extract field pairs
        pairs = re.findall(
            r'titulo_campo(?:_processo)?"[^>]*>(.*?)</div>\s*'
            r'<div[^>]*class="col-\d+\s+dados_campo(?:_processo)?[^"]*"[^>]*>(.*?)</div>',
            block, re.DOTALL
        )

        for label, value in pairs:
            cl = re.sub(r'<[^>]+>', '', label).strip()
            cv = re.sub(r'<[^>]+>', ' ', value).strip()
            cv = re.sub(r'\s+', ' ', cv)

            if cl.startswith('Tema Repetitivo'):
                continue
            if cl and cv:
                t[cl] = cv

        # Also try broader patterns for specific fields
        for field in ['Questão submetida a julgamento', 'Tese Firmada', 'Anotações NUGEPNAC',
                      'Delimitação do Julgado', 'Repercussão Geral', 'Situação do Tema']:
            if field not in t:
                pattern = re.escape(field) + r'.*?</div>\s*<div[^>]*>(.*?)</div>'
                m = re.search(pattern, block, re.DOTALL)
                if m:
                    val = re.sub(r'<[^>]+>', ' ', m.group(1)).strip()
                    val = re.sub(r'\s+', ' ', val)
                    if val:
                        t[field] = val

        # Normalize Situação
        if 'Situação' not in t and 'Situação do Tema' in t:
            t['Situação'] = t.pop('Situação do Tema')

        # Add link
        t['link'] = (
            f"https://processo.stj.jus.br/repetitivos/temas_repetitivos/"
            f"pesquisa.jsp?novaConsulta=true&tipo_pesquisa=T"
            f"&cod_tema_inicial={t['tema']}&cod_tema_final={t['tema']}"
        )

        themes.append(t)

    return themes

def main():
    all_themes = {}
    page_size = 100
    page_start = 1

    while True:
        url = (
            f"https://processo.stj.jus.br/repetitivos/temas_repetitivos/"
            f"pesquisa.jsp?novaConsulta=true&tipo_pesquisa=T"
            f"&l={page_size}&i={page_start}"
        )

        print(f"Fetching page starting at {page_start}...")
        try:
            html = fetch(url)
            themes = parse_page(html)

            if not themes:
                print(f"  No themes found, stopping.")
                break

            for t in themes:
                all_themes[t['tema']] = t

            print(f"  Found {len(themes)} themes (total unique: {len(all_themes)})")

            if len(themes) < page_size:
                break

            page_start += page_size
            time.sleep(0.5)

        except Exception as e:
            print(f"  Error: {e}")
            break

    # Sort by tema number
    result = sorted(all_themes.values(), key=lambda x: int(x['tema']))

    # Safety check: do NOT overwrite if extraction failed
    script_dir = os.path.dirname(os.path.abspath(__file__))
    out_path = os.path.join(script_dir, 'dados.json')

    if len(result) < 100:
        print(f"\nABORTADO: Apenas {len(result)} temas extraídos (mínimo: 100).")
        print("Os dados existentes NÃO foram alterados.")
        sys.exit(1)

    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\nTotal: {len(result)} temas salvos em {out_path}")
    print(f"Com tese: {sum(1 for t in result if t.get('Tese Firmada', '') not in ('', '-'))}")

    # Regenerate HTML
    generate_html(result, script_dir)

    return len(result)

def generate_html(data, base_dir):
    """Regenerate index.html with updated data."""
    html_path = os.path.join(base_dir, 'index.html')

    if not os.path.exists(html_path):
        print("index.html not found, skipping HTML generation")
        return

    with open(html_path, 'r', encoding='utf-8') as f:
        html = f.read()

    # Replace the data JSON in the HTML
    data_json = json.dumps(data, ensure_ascii=False)

    # Pattern: const D=[...]; (the data array)
    new_html = re.sub(
        r'const D=\[.*?\];',
        f'const D={data_json};',
        html,
        count=1,
        flags=re.DOTALL
    )

    if new_html != html:
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(new_html)
        print(f"index.html updated ({len(new_html):,} bytes)")
    else:
        print("Warning: Could not update data in index.html")

if __name__ == '__main__':
    main()
