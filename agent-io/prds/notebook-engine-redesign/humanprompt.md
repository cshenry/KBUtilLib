# Notebook Engine Redesign — Human Prompt

> Captured from `/ai-design` session, 2026-05-02 → 2026-05-03.
> User: Chris Henry (`chenry@anl.gov`).

## Initial framing

> "I want to do a critical re-envisioning of the notebook utilities in KBUtilLib. First, review the repo and specifically the notebook utility module."

## Vision (verbatim from session)

> "So to be clear, my vision is to reconceive the system. You see it in action. I acknowledge the flaws. My thought is let's reconceive a system that preserves the strengths... but builds a more robust formal provenanced foundation. Then we initiate a complete refactor of ADP1Notebooks with tests etc.
>
> It's worth looking at the 'jupyter-dev' skill to understand the principles of how the system works. Basically, I'm trying to break the utter chaos of unrestricted notebook building. I don't want notebooks that need to be rerun from cell 1 every single time. I want notebooks that can be run in the middle through the use of caching and use of the util module to aggregate and perform all inputs in one place. I want functions put in util rather than declared in notebook cells. Ultimately, the vision is functions in utils gets migrated to util modules in KBUtilLib or in ModelSEEDpy or CobraKBase etc. So I see util growing as temporary functions are made, then once tested, we move the functions to permanent homes.
>
> You have called out problems with the naming scheme and object cache that are very real. I have a specific more formal vision I want to implement with this. Specifically, I want notebooks to track an array of 'Samples' which are: (1) a media formulation; (2) a dictionary of strains and abundances (typically just a single strain and an abundance of 1 for pure cultures, but I want to support communities); (3) strain definitions — which is an MSGenome object + a list of mutations; (4) replicates and aggregated replicates (average). Samples should be associated with vectors of numbers which should have a type (metabolomics, proteomics relative abundance/log2, flux). Items in vectors can be associated with typed entities: model reaction, metabolite, gene. I want a true formal datastructure for these entities — not just a bunch of JSON files — maybe an SQLite or parquet? That would be used for numerical data. For other data to be stored in the cache, I wonder about the notebook subfolders... I think this is wrong? I think cache objects should have automatically maintained metadata (e.g. when it was created/modified/read, a list of notebooks and notebook cells reading/creating it, the type of the object). Then the object data could be a blob that could be JSON where appropriate, but other datastructures could be used for other datatypes (e.g. KBase objects, some form for numpy???, cobrapy models). Then we get rid of all these other subfolders (e.g. models etc). Then I want there always to be a manifest notebook that has a list of all subnotebooks in the project — when they were run — what cells need to be run — what data objects are created by the notebook and whether they are out of date. We could also create a utility API in notebook utils that allows you to intellectually browse/explore the notebook project's data.
>
> Obviously — the vision above is a comprehensive scientific data engine to underly our notebook framework. I think the visualization capabilities are also important, but I agree that belongs in a separate utility module.
>
> I'm open to refactoring KBUtilLib's composition-based vision — but this will be tricky and the improved data management seems more urgent.
>
> I would like to explore how a test system could be lashed onto this somehow..."

## Subsequent direction

> "We can and should change [the jupyter-dev] command as we reconceive how to write notebooks well."

(Confirmed: rewriting `/jupyter-dev` is part of the deliverable, scheduled as Phase 4.5.)
