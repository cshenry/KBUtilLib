# Add UniProt API Wrapper Module

I want to add a new utility modules that wraps the uniprot API. Specifically, I want to fetch information about uniprot entries based on a uniprot ID. The information I fetch should be flexible and specified as input arguments. this should include protein sequence, annotations, publications, Rhea IDs, PDB IDs, and critically, UniRef ID. The UniRef IDs are the most critical thing we need, so even if we need to hit another API to get those, I want to get those IDs.
