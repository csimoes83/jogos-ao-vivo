# jogos-ao-vivo

Liga o «ao vivo» e atualiza os resultados dos jogos da Liga Placard, Liga Feminina Placard e Liga Prime Futsal
no futsalportugal.com (24-26/10/2026), a partir de um GitHub Action. Não depende de nenhum computador ligado.

- `live.py` — o programa (só biblioteca standard do Python). `python3 live.py --selftest` corre os testes.
- `matches.json` — os 18 jogos, com a hora de Lisboa, o id no Flashscore e (Espanha) a página da RFEF.
- `.github/workflows/live.yml` — corre de 5 em 5 minutos nos dias dos jogos; também se pode lançar à mão
  (Actions → «jogos ao vivo» → Run workflow): `probe` só lê, `dry` pede dry-run ao site, `run` aplica.

Fontes: Flashscore (Portugal e Espanha) e RFEF (confirmação em Espanha). A FPF bloqueia os servidores do GitHub.
Segredo necessário: `NFP_MCP_TOKEN` (token do site com permissões de leitura e escrita MCP, sem admin).

Depois de 26/10: desativar ou apagar o repositório.
