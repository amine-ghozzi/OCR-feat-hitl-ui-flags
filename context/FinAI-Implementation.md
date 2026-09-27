# FinAI — suivi de développement

Date : 27 septembre 2026. Statut : premier lot développé, non déployé.

## Référence approuvée

Le [compte rendu de compréhension, plan et maquettes](Documentation/FinAI-Compte-rendu-et-plan-interface.html)
est intégré à la documentation globale et référencé depuis le README et le Master Plan.
Le document de cadrage est conservé tel qu’approuvé ; ce suivi distingue la réalisation du projet cible.

## Fonctions de ce lot

- Contrôle d’admissibilité avant la cascade comptable, en modes local et serveur.
  Lecture de toutes les pages natives, sans décision limitée à la couverture.
  Pour les scans et lectures incomplètes, première lecture Docling réutilisée par le niveau OCR.
- Périmètre initial : bilan et compte de résultat, signatures françaises/anglaises.
  Les tableaux de flux seuls ne sont pas admis dans ce lot.
- Causes distinctes : format non pris en charge, fichier corrompu/protégé,
  document hors périmètre, lecture insuffisante/ambiguë, extraction sans tableau exploitable.
  Un rejet d’admissibilité termine le job avec `failed`, une cause et une action ;
  il ne déclenche pas les trois nouvelles tentatives prévues pour les erreurs techniques.
- Événements horodatés réels : identification, admissibilité, moteurs d’extraction,
  réconciliation, cadrage et attente de validation. Aucune estimation de pourcentage.
  Les traitements internes de mapping, scoring et règles restent regroupés dans ce lot.
- API : champ additionnel `latest_job.progress`, stocké dans le JSON existant du job.
  Aucune migration SQL nécessaire. Les réponses de suivi n’exposent pas le résultat financier complet.
  Une nouvelle tentative efface le suivi précédent ; un événement tardif ne remplace pas un résultat terminé.
- Interface : accueil et parcours principal en français, journal du traitement,
  lecture source brute séparée des valeurs interprétées/traitées, erreurs persistantes,
  effacement du résultat précédent lors d’une nouvelle analyse.
- Captures des textes et tableaux avant corrections lexicales/comptables pour PyMuPDF et Docling ;
  colonnes d’origine conservées, copie détachée, empreinte SHA-256, vue en lecture seule.
  Les tableaux bruts sont présentés dans leur inventaire source, sans fausse correspondance
  avec les indices des tableaux réorganisés par l’ensemble de moteurs.
- DOCX : prise en charge des tableaux natifs en complément des images intégrées.
- Bouton `Masquer le suivi` : son libellé indique correctement qu’il n’annule pas le traitement serveur.
- Upload local : noms temporaires uniques ; stockage API : séparateurs Windows normalisés,
  corrigeant un défaut préexistant détecté par les tests de sécurité.

## Limites explicites et prochains lots

1. L’admissibilité utilise des indices déterministes (titre, vocabulaire comptable, montants),
   pas un classifieur calibré sur le corpus. Elle peut refuser un document financier atypique
   ou admettre un texte contenant suffisamment d’indices. Recette métier indispensable avant production.
   Le diagnostic de lecture ne constitue pas un score qualité calibré.
2. La lecture OCR préalable des scans peut rester longue. Aucun compteur page par page ni
   délai garanti n’est affiché. Le moteur fournit sa réponse avant les événements suivants.
3. L’ordre actuel des traitements comptables est conservé : le cadrage consolidé suit la cascade.
   La séquence cible à neuf étapes nécessite une décomposition ultérieure du moteur.
4. À développer : sélection interactive des pages/états, exercices et champs à extraire,
   puis mapping L1 figé avec paramètres et traçabilité complète des versions.
5. Les captures brutes sont des sorties de moteur, pas les pixels originaux : PyMuPDF a déjà
   reconstruit la géométrie des tableaux. L’empreinte détecte une modification ; elle ne constitue
   pas une protection matérielle contre l’écriture. Les anciens résultats n’ont pas de capture brute.
   Le résultat d’un ensemble conserve la capture du moteur retenu comme base, pas un registre de tous les moteurs.
6. Les scores et règles existants restent disponibles. Leur affichage dédié à chaque étape,
   la traduction complète des écrans avancés et le comparatif cellule par cellule restent à terminer.
7. La reconstruction et la réévaluation du résultat API final après corrections humaines,
   ainsi que l’annulation effective et la reprise d’un traitement, restent à implémenter.
   La fin de l’extraction ne signifie jamais que le document a été validé par un humain.

## Vérification

Voir [résultats de vérification](FinAI-Test-Results.md). Les tests de serveur utilisent SQLite
et un transport HTTP en mémoire ; aucun déploiement de la stack Docker ni chargement des modèles
OCR lourds n’a été effectué. Les lectures natives sont testées avec de vrais PDF/DOCX générés.
Les chemins OCR sont testés avec des sorties de moteur simulées.

## Mise en service pour recette

Utiliser la procédure Docker existante du README et sa configuration des secrets.
Reconstruire ensemble les images `api`, `worker` et `ui` pour conserver le contrat de suivi.
Ne pas valider cette livraison en production avant une recette du corpus de documents réels,
notamment scans, états multipages, titres atypiques et refus motivés.
