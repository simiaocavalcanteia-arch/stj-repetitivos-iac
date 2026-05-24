#!/usr/bin/env python3
"""
Script de atualização automática dos dados de Repetitivos e IACs do STJ.
Extrai dados de https://processo.stj.jus.br/repetitivos/temas_repetitivos/

Usa Playwright (Chromium headless) para superar o JS challenge do F5 BIG-IP
que protege o site (cookies TS*).
"""
import re, json, os, sys, time, random
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE = "https://processo.stj.jus.br/repetitivos/temas_repetitivos/"

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36")


def parse_page(html):
    themes = []
    blocks = html.split('containerDocumento')

    for block in blocks[1:]:
        t = {}

        m = re.search(r'dados_campo_processo\s+fonte_destaque[^>]*>\s*(\d+)', block)
        if not m:
            continue
        t['tema'] = m.group(1)

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

        if 'Situação' not in t and 'Situação do Tema' in t:
            t['Situação'] = t.pop('Situação do Tema')

        t['link'] = (
            f"https://processo.stj.jus.br/repetitivos/temas_repetitivos/"
            f"pesquisa.jsp?novaConsulta=true&tipo_pesquisa=T"
            f"&cod_tema_inicial={t['tema']}&cod_tema_final={t['tema']}"
        )

        themes.append(t)

    return themes


def fetch_all_themes():
    """Usa Chromium headless para superar o JS challenge do F5 e extrair HTML."""
    all_themes = {}
    page_size = 100

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=['--disable-blink-features=AutomationControlled'],
        )
        context = browser.new_context(
            user_agent=USER_AGENT,
            locale='pt-BR',
            timezone_id='America/Sao_Paulo',
            viewport={'width': 1366, 'height': 900},
        )
        page = context.new_page()

        # Warmup: passa pelo JS challenge do F5 carregando a home
        print("Warmup: acessando home para resolver JS challenge do F5...")
        page.goto(BASE, wait_until='networkidle', timeout=120_000)
        cookies = context.cookies()
        ts_cookies = [c['name'] for c in cookies if c['name'].startswith('TS')]
        print(f"  Cookies F5/TS obtidos: {ts_cookies}")
        if not ts_cookies:
            print("  AVISO: nenhum cookie TS* — possível que o challenge não tenha rodado.")

        page_start = 1
        while True:
            url = (
                f"{BASE}pesquisa.jsp?novaConsulta=true&tipo_pesquisa=T"
                f"&l={page_size}&i={page_start}"
            )
            print(f"Fetching page starting at {page_start}...")

            ok = False
            for attempt in range(1, 4):
                try:
                    resp = page.goto(url, wait_until='networkidle', timeout=120_000)
                    status = resp.status if resp else 'no-response'
                    if status == 200:
                        ok = True
                        break
                    print(f"  Tentativa {attempt}/3 retornou {status}, aguardando...")
                    time.sleep(3 + attempt * 2)
                except PWTimeout as e:
                    print(f"  Timeout tentativa {attempt}/3: {e}")
                    time.sleep(3 + attempt * 2)

            if not ok:
                print("  Falha ao obter página após 3 tentativas, abortando loop.")
                break

            html = page.content()
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
            time.sleep(0.8 + random.uniform(0, 0.6))

        browser.close()

    return all_themes


def main():
    all_themes = fetch_all_themes()
    result = sorted(all_themes.values(), key=lambda x: int(x['tema']))

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

    data_json = json.dumps(data, ensure_ascii=False)

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
