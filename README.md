# Funding Arcus / Lighter sur Robinhood Chain

Bot d'arbitrage de funding entre Arcus et
[Lighter sur Robinhood Chain](https://robinhoodchain.lighter.xyz/), sur les
contrats d'actions et de matières premières communs aux deux plateformes.
L'environnement cible est le **mainnet**, sans testnet.

Python 3.11–3.13 (3.12 recommandé sur Ubuntu). La stratégie est dans `main.py`,
les appels aux plateformes dans `exchanges.py`.

## Installation et lancement

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

export ARCUS_ADDRESS='0x...'
export ARCUS_SIGNING_KEY='...'
export ARCUS_ACCOUNT_INDEX='0'
export LIGHTER_ACCOUNT_INDEX='...'
export LIGHTER_API_KEY_INDEX='...'
export LIGHTER_API_PRIVATE_KEY='...'

export POSITION_USD='200'
export MAX_PAIRS='3'
.venv/bin/python main.py
```

Le SDK officiel `lighter-sdk` est fixé à la version **1.1.6**. L'adaptateur
utilise uniquement le mainnet Robinhood Chain, avec le chain ID **466324**.
Une autre URL est refusée au démarrage, y compris les endpoints testnet.
Le journal d'une ancienne intégration n'est pas réutilisé : il appartient
aux comptes et plateformes qui l'ont créé. Fermer les anciennes positions
avant de choisir un nouveau fichier `STATE_FILE`.

## Connexion mainnet

| Service | Adresse |
|---|---|
| Interface Lighter sur Robinhood Chain | `https://robinhoodchain.lighter.xyz/` |
| API REST Lighter mainnet | `https://api.rh.lighter.xyz/` |
| Documentation Lighter | `https://apidocs.rh.lighter.xyz/docs/get-started` |
| Référence API | `https://apidocs.rh.lighter.xyz/reference` |
| Spécification OpenAPI | `https://apidocs.rh.lighter.xyz/openapi/api.json` |
| SDK Python officiel | `https://github.com/elliottech/lighter-python` |
| API REST Arcus mainnet | `https://api.arcus.xyz` |

Le package Python officiel est `lighter-sdk`. Les ordres sont signés avec
`lighter.SignerClient`, initialisé avec l'URL de l'API, l'index du compte et
une clé privée API associée à son index. Utiliser l'URL de l'API REST pour le
SDK ; l'URL de l'interface sert à accéder à la plateforme dans le navigateur.

Retrouver l'index du compte avec
`AccountApi.accounts_by_l1_address(l1_address=...)`. Créer une clé API dédiée
à ce compte. Les index 0 à 3 et 157 sont réservés aux interfaces web/mobile ;
utiliser un index autorisé dans les plages 4–156 ou 158–254.

Configuration de l'adaptateur Lighter :

```bash
export LIGHTER_BASE_URL='https://api.rh.lighter.xyz/'
export LIGHTER_ACCOUNT_INDEX='...'
export LIGHTER_API_KEY_INDEX='...'
export LIGHTER_API_PRIVATE_KEY='...'
```

Arcus utilise `ARCUS_ADDRESS`, `ARCUS_ACCOUNT_INDEX` et `ARCUS_SIGNING_KEY`.
Cette dernière est la clé privée **API Signing Key** Ed25519 enregistrée dans
API Keys, pas la clé du wallet Ethereum. Alimenter les deux comptes et les
dédier au bot. Il refuse les ordres existants et les positions qu'il n'a pas
enregistrées dans son journal.

## Entrée et sélection

- Actions et matières premières communes uniquement, excluant les cryptos,
  indices et FX. Vérifier l'identité exacte du sous-jacent et les caractéristiques
  du contrat. Pas de substitution ETF/métal : GLD n'est pas XAU. Les marchés
  disponibles sur Robinhood Chain doivent être vérifiés auprès de son API.
- Taux horaires en fractions : `nextFundingRate` sur Arcus et
  `current_funding_rate / 100` sur Lighter (pourcentage horaire du WebSocket
  `market_stats/all`). Un taux positif est payé par les longs.
  Shorter le taux le plus élevé et longer le plus faible, même si les deux
  sont négatifs.
- Ecart relatif : `abs(Lighter - Arcus) / max(abs(Lighter), abs(Arcus))`.
  Il doit etre >=30 %, dans le meme sens pendant les quatre dernieres heures.
  Avant chaque ouverture, les historiques publics horaires sont recuperes via
  `fundingRates` (Arcus) et `fundings` (Lighter). Les heures sont alignees et
  le point precedent le debut de la fenetre est inclus : cinq taux horaires
  sont verifies. Toute heure manquante, donnee invalide, inversion ou passage
  sous le seuil bloque l'entree. Une API indisponible bloque cette entree.
  Les taux regles horaires ne prouvent pas la stabilite des taux indicatifs
  entre deux heures. Le bot complete ce controle par ses observations toutes
  les 30 secondes ; une inversion ou un ecart insuffisant observe bloque
  les nouvelles entrees pendant quatre heures, y compris apres redemarrage.
  L'historique de classement conserve quatre heures ; une interruption
  >90 secondes le reinitialise. L'API permet l'entree des le demarrage si
  l'historique est conforme et qu'aucune observation locale ne l'invalide.
- Classement : toutes les paires communes admissibles sont comparees avant
  toute ouverture, a partir des historiques API des deux plateformes.
  Le score est la moyenne de `abs(Lighter - Arcus)` sur exactement quatre
  heures, ponderee par la duree couverte par chaque taux horaire, sans penalite
  de volatilite. Il compare l'ecart absolu des taux, pas l'ecart relatif de 30 %.
  Ouvrir les meilleures paires dans l'ordre decroissant, selon les places
  disponibles et la liquidite. L'historique est revalide avant chaque ouverture.
  Les positions existantes sont conservees jusqu'a un critere de sortie.
  Le score ne predit pas les fundings futurs ni le rendement net de frais.
- Spread bid/ask <=0,03 % sur chacun des deux exchanges, mesure par
  `(ask - bid) / ((ask + bid) / 2)`. Aucun filtre sur l'ecart de prix entre
  exchanges. Verifier la profondeur pour la quantite entiere et relire le
  carnet juste avant ouverture. Le slippage de profondeur conserve son
  seuil distinct de 0,05 % a l'entree.
- Environ 200 $ de notionnel par jambe, indépendamment du levier. Utiliser la
  même quantité d'actif des deux côtés, arrondie au pas commun. Ignorer la
  paire si les minimums ne permettent pas cette taille.
- Maximum de trois paires distinctes ; conserver une paire ouverte jusqu'à
  ce qu'un critère de sortie soit atteint.

Le bot choisit le levier entier maximal déduit des exigences de marge du
marché, sur chaque plateforme, en marge isolée. La marge initiale est donc
approximativement le notionnel divisé par ce levier. À 20×, une jambe de
200 $ demande environ 10 $ de marge initiale, soit 30 $ par plateforme
pour trois paires ; à 5×, il faut environ 120 $. Prévoir en plus les frais,
les fundings et les pertes sur chaque plateforme. Un solde de 300 $ par
plateforme couvre ces exemples, mais la marge requise dépend des marchés.
Le bot ne vérifie pas le solde disponible avant ouverture et ne répartit
pas les dépôts supplémentaires entre les marges isolées : déposer davantage
ne protège donc pas automatiquement les positions existantes de la liquidation.

Confirmer le levier et le mode de marge avant ouverture. Arcus utilise la
marge isolée. Sur Lighter, les précisions de prix et de quantité proviennent
de `orderBookDetails`. Le SDK attend des prix et quantités convertis en
entiers selon ces précisions. Les ordres MARKET/IOC utilisent le prix comme
limite d'exécution acceptable. La limite de slippage prévue à l'entrée est
de 0,05 %.

Les ordres sont envoyés en deux étapes : Lighter MARKET/IOC d'abord,
puis Arcus LIMIT/FOK après confirmation du fill Lighter complet. Les deux
jambes ne sont donc pas atomiques : le bot est temporairement exposé entre
les fills. L'entree ne depend pas de la comparaison entre mark et oracle.
Un ACK ne suffit pas : le statut terminal et la position sont confirmés.
Si Lighter est indisponible et que le hedge est impossible, annuler tout
ordre d'entrée encore actif. Si une jambe a déjà été exécutée, la refermer en
reduce-only. En cas de quantités différentes, refermer les expositions et
arrêter les nouvelles entrées. Conserver les références des ordres incertains
jusqu'à confirmation de leur statut terminal.

## Sorties

Fermer les deux jambes en taker, avec `reduceOnly`, si :

- le funding horaire combiné dans le sens détenu devient nul ou négatif,
  en tenant compte du mark price propre à chaque jambe, uniquement pendant
  les deux dernieres minutes de l'heure (`hh:58:00` a `hh:59:59`). Une
  inversion a `10:02` ne provoque aucune cloture pour funding avant `10:58`.
  Si le funding est redevenu positif au controle, la position est conservee ;
- le prix d'une jambe s'écarte de ±10 % de son entrée ;
- il reste au plus 10 % de la distance entrée → liquidation à parcourir
  sur une jambe : entrée 100, liquidation 95 → sortie dès 95,50 ;
- une position disparaît, la couverture devient non neutre ou les données
  de risque deviennent indisponibles.

Risques vérifiés environ toutes les 2 secondes ; fundings toutes les 30 secondes.
La sortie pour funding est controlee a chaque scan dans la fenetre de fin
d'heure, soit un premier controle normalement entre `hh:58:00` et `hh:58:30`.
Les appels réseau peuvent rallonger ces délais. Arcus n'expose pas de prix de
liquidation : il est estimé en marge isolée à partir de
`marginUsed + borrowedCapital`, de la quantité signée et de
`maintenanceMarginFraction`. Lighter utilise la marge isolée, son mark actualisé
et le champ `liquidation_price` du compte pour les contrôles de risque.
Les gaps de prix et indisponibilités peuvent
empêcher une fermeture avant liquidation.

Le filtre de spread est verifie sur les deux carnets avant ouverture, puis
sur le carnet frais de chaque jambe juste avant son ordre. Il ne s'applique
pas aux clotures reduce-only. `STABILITY_HOURS` n'est plus utilise, meme si
cette variable est encore definie sur le serveur. Un ancien `MAX_SPREAD`
superieur a `0.0003` ne permet pas de depasser le plafond de 0,03 %.

Les fermetures acceptent jusqu'à 2 % de slippage et peuvent être partielles.
Vérifier la position résiduelle et tenter jusqu'à cinq fermetures reduce-only.
Une plateforme en erreur ne doit pas empêcher une tentative sur l'autre.
Le seuil relatif de funding ne garantit pas l'amortissement des frais et spreads.

## Journal et arrêt

`funding-state.json` conserve l'historique observé et les paires actives,
enregistrées avant le premier envoi d'ordres. Il est lié aux comptes et
environnements utilisés, sans contenir leurs clés. Conserver ce fichier sur
le serveur. Les opérations interrompues sont refermées après redémarrage.
Un ordre sans statut terminal confirmé bloque les nouvelles entrées.

`funding-state.orders.jsonl` (chemin dérivé de `STATE_FILE`) conserve chaque
demande d'ordre, son identifiant durable, son résultat et son motif, ainsi que
les demandes d'annulation. Avant fermeture, un snapshot enregistre le PnL
latent par plateforme.

À la clôture, le bilan `pair_closed` distingue pour chaque exchange le PnL
réalisé de prix, le funding reçu/payé, les frais et le total net. Arcus utilise
`fills.closedPnl` (déjà net de frais) et `fundingPayments.payment`. Lighter
utilise les PnL des trades privés, leurs taux de frais en millionièmes et
`positionFunding.change`. Les historiques sont limités à la durée de la paire.
Le bilan et les références restent aussi dans la liste `closed` du fichier
d'état. Les frais sont déduits une seule fois.

Si un historique est indisponible, tronqué ou ne contient pas encore les fills
confirmés, le bilan indique `unavailable` et des valeurs `null`, sans inventer
un PnL nul. Cela n'empêche pas la fermeture des positions. Le bilan enregistré
n'est pas recalculé automatiquement lorsque ces données redeviennent disponibles.

`Ctrl+C` ou SIGTERM déclenchent une tentative de fermeture des
paires enregistrées. Si elle échoue, le journal reste conservé et un message
URGENT demande une vérification manuelle. Un crash brutal ou un serveur éteint
laisse les positions ouvertes. Ne lancer qu'une instance sur ces comptes
et ce journal.

Une erreur de lecture des positions suspend les nouvelles entrées sans fermer
les paires saines. Les lectures sont retentées à chaque cycle. Après 10 secondes
de panne, si une jambe lisible ne correspond plus à la quantité attendue, la
paire est fermée en reduce-only ; une fermeture incomplète est retentée aux
cycles suivants. Une plateforme illisible ne vaut jamais une position nulle :
le delta réel ne peut pas être confirmé tant que les deux lectures ne sont pas
disponibles. Un déséquilibre confirmé sur les deux plateformes déclenche la
fermeture immédiatement. Une erreur fatale conserve le journal sans fermer
automatiquement toutes les paires ; le redémarrage réconcilie les opérations.

Après chaque fermeture, le même symbole ne peut pas être rouvert pendant
10 minutes. Cette temporisation est conservée dans le journal, y compris
après redémarrage.

## Paramètres de stratégie

| Variable | Défaut | Sens |
|---|---|---|
| `POSITION_USD` | `200` | Notionnel maximal par jambe |
| `MAX_PAIRS` | `3` | Nombre maximal de paires ouvertes |
| `MIN_FUNDING_GAP` | `0.30` | Écart relatif de funding |
| `MAX_SPREAD` | `0.0003` | Spread bid/ask uniquement ; plafond 0,03 %, configurable plus bas |
| `STATE_FILE` | `funding-state.json` | Fichier d'état |
| `ARCUS_BASE_URL` | `https://api.arcus.xyz` | API Arcus mainnet uniquement |
| `LIGHTER_BASE_URL` | `https://api.rh.lighter.xyz` | API Robinhood Chain mainnet uniquement |
| `LIGHTER_ACCOUNT_INDEX` | requis | Index du compte Lighter |
| `LIGHTER_API_KEY_INDEX` | requis | Index de la clé API Lighter |
| `LIGHTER_API_PRIVATE_KEY` | requis | Clé privée API Lighter |

Si `MIN_FUNDING_GAP` est deja defini dans l'environnement de lancement,
le passer a `0.30` : cette variable remplace le defaut du code.

## Vérifications

```bash
python -m unittest -v
```

Les tests couvrent les fills partiels, les pannes de plateforme, les annulations,
les placements incertains après redémarrage, les entrees independantes du mark/oracle, la précision
des ordres, la conversion du funding et les bilans de PnL. Une signature
Lighter MARKET/IOC sur Robinhood Chain est construite hors ligne avec une clé
fictive. Les marches communs, les carnets publics et le flux de funding ont
été vérifiés sur les APIs mainnet. Aucun ordre réel n'a ete envoyé ; les écritures
authentifiées sont testées avec des simulations.

Documentation : [Arcus](https://docs.arcus.xyz/guides/websocket-trading),
[Lighter sur Robinhood Chain](https://apidocs.rh.lighter.xyz/docs/get-started),
[SDK Python officiel](https://github.com/elliottech/lighter-python).
