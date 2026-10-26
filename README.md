# Market making XAU sur Lighter

Bot limité au contrat perpétuel **XAU** de **Lighter sur Robinhood Chain**,
mainnet, chain ID **466324**. La stratégie est dans `main.py`, les appels
Lighter dans `exchanges.py`. Les intégrations Arcus et l'arbitrage de funding
ont été retirés.

## Règles

- À plat : un achat au meilleur bid et une vente au meilleur ask, en
  **LIMIT / POST_ONLY**. Un mouvement du best bid/ask provoque une annulation
  confirmée puis un remplacement. Aucun passage taker pour les quotes.
- Taille cible : **10 $ de notionnel**, avec la même quantité XAU des deux
  côtés pour permettre à l'ordre opposé de solder la position.
- Un tick doit contenir au moins **100 $ de liquidité des autres comptes**.
  Toutes les quantités au même prix sont additionnées ; les ordres de notre
  compte sont exclus. Le contrôle porte sur le côté de notre ordre.
  À exactement 100 $, l'ordre est conservé ; sous 100 $, il est annulé.
- Dès un fill, même partiel : annuler le reliquat dans le sens de la
  position et bloquer les nouvelles entrées de ce côté. Il n'y a pas de
  cumul d'ordres pour augmenter l'inventaire.
- Après un fill complet, conserver l'ordre opposé à son prix initial tant
  que son tick reste admissible, même si le meilleur prix se déplace.
  Après un fill partiel, si cet ordre peut dépasser l'inventaire restant,
  le recréer au même prix pour la quantité restante, en **reduce-only**.
- Le seuil de liquidité s'applique également à la sortie. Si son tick
  devient inadmissible, annuler l'ordre ; le replacer au meilleur prix
  admissible dès que possible. Le stop continue à être surveillé.
- Stop à **1 $ de variation du prix XAU**, par rapport au prix moyen
  d'entrée exécuté : long si le meilleur bid est inférieur ou égal à
  entrée − 1 ; short si le meilleur ask est supérieur ou égal à entrée + 1.
  Exemple : achat à 3000,45 → déclenchement à un bid de 2999,45.
- Au stop : annuler les quotes, confirmer leurs exécutions éventuelles,
  relire la position, puis fermer en **MARKET / IOC / reduce-only**.
  La limite d'exécution autorise 2 % de slippage ; un reliquat est retenté
  sans envoyer une nouvelle fermeture tant que la précédente est incertaine.
- Après confirmation d'une position nulle et annulation des reliquats,
  recommencer à coter dans les deux sens.

Le stop est **géré par le bot**, pas enregistré comme ordre conditionnel sur
Lighter. Il permet de conserver l'ordre opposé existant, puis de l'annuler
avant la fermeture pour éviter un retournement. Le bot doit rester actif
et connecté pour déclencher le stop. Le seuil de déclenchement ne garantit
pas le prix d'exécution.

## Taille de 10 $ et minimums du marché

L'API publique XAU a été consultée pendant la refonte : minimum de **10 $**,
quantité minimale **0,0020 XAU**, pas **0,0001 XAU**, tick de prix **0,01 $**.
Les valeurs sont relues depuis l'API au démarrage.

Par défaut, **10 $ est une taille cible** : le bot utilise le premier pas
de quantité qui atteint 10 $ au bid, avec la même quantité à l'ask.
L'arrondi est activé par défaut (`ROUND_ORDER_UP=1`). Par exemple, autour
de 4020 $, 0,0024 XAU valent environ 9,65 $ et 0,0025 XAU environ 10,05 $.
Dans ce mode, le plafond d'inventaire est la quantité de cette unique paire
de quotes arrondie ; sa valeur peut dépasser 10 $ et fluctue avec le prix.
`ROUND_ORDER_UP=0` réactive le plafond strict de 10 $. Ce mode peut bloquer
les entrées : avec le minimum actuel de 10 $ et deux prix différents pour
une même quantité, les deux quotes ne peuvent pas toutes deux être exactement
à 10 $. Si le service définit déjà cette variable à `0`, la passer à `1`.

Les sorties reduce-only utilisent la quantité réelle restante, qui peut
être inférieure au minimum d'une nouvelle entrée après un fill partiel.

## Installation et lancement

Python 3.11–3.13, SDK officiel `lighter-sdk==1.1.6`.

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

export LIGHTER_ACCOUNT_INDEX='...'
export LIGHTER_API_KEY_INDEX='...'
export LIGHTER_API_PRIVATE_KEY='...'
export STATE_FILE='xau-mm-state.json'

# Valeur par défaut ; remplace un éventuel ancien réglage à 0 :
export ROUND_ORDER_UP='1'

.venv/bin/python main.py
```

L'API est `https://api.rh.lighter.xyz` ; l'interface est
https://robinhoodchain.lighter.xyz/. Les autres endpoints sont refusés.
Les index de clés autorisés sont 4–156 et 158–254.

Utiliser un compte dédié à ce bot. Au démarrage, les ordres externes et
positions non réconciliées avec le journal bloquent les nouvelles entrées.
Le levier reste en marge isolée, avec une marge initiale cible supérieure
de 50 % au minimum du marché, levier entier arrondi vers le bas.
La configuration de marge est confirmée avant la première entrée.

Les anciennes variables `ARCUS_*`, `POSITION_USD`, `MAX_PAIRS`,
`MIN_FUNDING_GAP` et `MAX_SPREAD` ne sont plus utilisées.
Si `STATE_FILE` désigne encore un journal d'arbitrage, choisir le nouveau
journal XAU. Un journal d'une autre stratégie ou d'un autre compte est refusé.

## Exécution, reprise et arrêt

Le bot interroge le carnet, les ordres et la position par REST, avec une
pause de **0,5 seconde entre les cycles**. Les temps réseau et les délais
de confirmation s'ajoutent à cette pause. Ce n'est pas un flux WebSocket :
les annulations et le stop interviennent au moment de l'observation.

Les quotes expirent après 60 secondes si elles ne sont plus suivies.
Une quote expirée peut être recréée après réconciliation.
Le carnet contient au maximum les 250 premiers ordres par côté ; une
liquidité absente du snapshot est traitée comme insuffisante.

`xau-mm-state.json` enregistre les identifiants avant chaque envoi, les
fills confirmés, l'inventaire attendu et une fermeture en cours.
Il est remplacé atomiquement. Une réponse perdue ne provoque jamais un
renvoi automatique du même placement. Tant que fills et position ne
concordent pas, les nouvelles cotations restent suspendues.

Les erreurs de lecture déclenchent une tentative d'annulation des quotes.
Au redémarrage, les anciennes quotes du journal sont annulées et les
positions réconciliées avant reprise. Un ordre invisible ou une annulation
incertaine bloque tout remplacement.

`xau-mm-state.orders.jsonl` conserve les demandes d'ordres, fills,
annulations, déclenchements du stop et cycles terminés.
Un verrou sur `xau-mm-state.lock` interdit deux processus utilisant le même
journal. Garder un seul processus et un seul journal pour ce compte.

`Ctrl+C` / SIGTERM annulent les quotes puis tentent de solder l'inventaire.
Si la fermeture n'est pas confirmée, le journal est conservé et un message
URGENT est affiché. Un arrêt brutal peut laisser une position ouverte.

## Vérifications

```bash
python -m unittest -v
```

La suite utilise des plateformes simulées pour les fills complets et
partiels, limites d'inventaire, seuil de liquidité, stops dans les deux sens,
courses entre fill et annulation, réponses perdues et redémarrages.
Une signature POST_ONLY XAU est aussi construite avec le SDK réel,
hors ligne et avec une clé fictive. Aucun ordre réel n'a été envoyé
pendant ces vérifications.
