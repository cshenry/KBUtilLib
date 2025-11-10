# Protein PLM API Utility - Original Request

I want to implement a new utility module that interfaces with our Protein Language Model API.

The documentation for the API is at:
- https://kbase.us/services/llm_homology_api/docs#/
- https://github.com/bio-boris/protein_happi

This tool should accept a feature container object as input. The tools should retrieve the top 100 hits from the PLM API along with the sequences for those hits, construct a custom BLAST database with those hits, and then BLAST those hits. Then, match each feature to the hit that is the closest based on the BLAST. The tool should then return the Uniprot IDs of the best hits.
