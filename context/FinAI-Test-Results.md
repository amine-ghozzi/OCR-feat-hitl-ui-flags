# Vérifications — premier lot FinAI

27 septembre 2026.

## Résultats exécutés

- Suite ciblée de régression : **143 tests réussis**, un avertissement de dépréciation HTTP 422 dans FastAPI.
- Après les derniers ajustements de lecture DOCX et de validation de format :
  **20 tests du contrôle d’entrée réussis**, dont deux nouveaux tests DOCX.
  Ces tests recouvrent ceux déjà présents dans la suite précédente (145 tests distincts au total).
- Démarrage de l’interface avec `streamlit.testing.v1.AppTest` : aucune exception,
  onglets « Analyse documentaire » et « Historique » présents.
- Compilation Python des modules modifiés réussie.

## Commande de régression

```bash
python -m pytest tests/test_document_intake.py tests/test_unit_detector.py \
  tests/test_docling_confidence.py tests/test_docling_rotation.py \
  tests/test_tier0_geometry.py tests/test_tier0_fragments.py tests/test_tier1_full.py \
  tests/test_worker_runtime.py tests/test_api_integration.py tests/test_api_review.py \
  tests/test_api_security.py tests/test_review_view.py -q
```

Dernière vérification ciblée : `python -m pytest tests/test_document_intake.py -q`.

## Comportements couverts par les nouveaux tests

Admission bilan/résultat ; facture rejetée sans OCR ; lecture ambiguë distincte du hors-périmètre ;
état financier situé après quatre pages de couverture ; scan et réutilisation de sa première lecture ;
scan vide ; tableaux DOCX natifs ; corruption PDF/DOCX ; arrêt avant cascade comptable ;
séquence des événements ; exposition API du suivi sans le résultat financier ;
absence de retry métier ; stockage du suivi et protection du résultat terminé contre un événement tardif ;
capture avant corrections et préservation des colonnes dupliquées.

## Portée et limites

API testée via HTTP en mémoire et SQLite. Les PDF et DOCX des nouveaux tests sont réels,
mais synthétiques. Les réponses des moteurs OCR lourds sont simulées. Pas de validation Docker,
RabbitMQ, MinIO, PostgreSQL, ni recette des modèles OCR sur l’ensemble du corpus utilisateur.
Le test Streamlit vérifie l’exécution de la page, pas une recette visuelle complète dans un navigateur.
La suite complète historique du dépôt n’a pas été exécutée.

Le premier passage élargi avait détecté le défaut préexistant de séparateurs Windows dans
`storage._logical_key` ; il a été corrigé et son test passe dans les 143 résultats ci-dessus.
