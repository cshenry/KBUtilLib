/*
@author chenry, jayrbolton, slebras, psdehal, scanon, jkbaumohl, seanjungbluth, dylan
*/
module KBaseGenomes {
    /*
    Reference to a ContigSet object containing the contigs for this genome in the workspace
    @id ws KBaseGenomes.ContigSet
    */
    typedef string ContigSet_ref;

    /*
    Reference to a ProteinSet object containing the proteins for this genome in the workspace
    @id ws KBaseGenomes.ProteinSet
    */
    typedef string ProteinSet_ref;

    /*
    Reference to a TranscriptSet object containing the transcripts for this genome in the workspace
    @id ws KBaseGenomes.TranscriptSet
    */
    typedef string TranscriptSet_ref;

    /*
    Reference to a Feature object of a genome in the workspace
    @id subws KBaseGenomes.Genome.features.[*].id
    */
    typedef string Feature_ref;

    /*
    Reference to a Genome object in the workspace
    @id ws KBaseGenomes.Genome KBaseGenomeAnnotations.GenomeAnnotation
    */
    typedef string Genome_ref;

    /*
    Reference to an Assembly object in the workspace
    @id ws KBaseGenomeAnnotations.Assembly
    */
    typedef string Assembly_ref;

    /*
    Reference to a Pangenome object in the workspace
    @id ws KBaseGenomes.Pangenome
    */
    typedef string Pangenome_ref;

    /*
    Reference to a Proteome Comparison object in the workspace
    @id ws GenomeComparison.ProteomeComparison
    */
    typedef string Protcomp_ref;

    /*
    Name of a Genome object in the workspace
    */
    typedef string Genome_name;

    /*
    Reference to a source_id
    @id external
    */
    typedef string source_id;

    /*
    KBase legacy data ID
    @id kb
    */
    typedef string Genome_id;

    /*
    KBase Reaction ID
    @id external
    */
    typedef string Reaction_id;

    /*
    KBase Feature ID
    @id external
    */
    typedef string Feature_id;

    /*
    KBase ProteinSet ID
    @id kb
    */
    typedef string ProteinSet_id;

    /*
    ProbabilisticAnnotation ID
    @id kb
    */
    typedef string ProbabilisticAnnotation_id;

    /*
    Genome protein ID
    @id external
    */
    typedef string Protein_id;

    /*
    Reference to an individual contig in a ContigSet object
    @id subws KBase.ContigSet.contigs.[*].id
    */
    typedef string Contig_ref;

    /*
    ContigSet contig ID
    @id external
    */
    typedef string Contig_id;

    /*
    KBase contig set ID
    @id kb
    */
    typedef string ContigSet_id;

    /*
    Reference to a reads file in shock
    @id shock
    */
    typedef string Reads_ref;

    /*
    Reference to a fasta file in shock
    @id shock
    */
    typedef string Fasta_ref;

    typedef string Feature_type;

    typedef int Bool;

    /*
    Type spec for a "Contig" subobject in the "ContigSet" object

    Fields:
        id - Contig_id - ID of contig in contigset
        md5 - string - unique hash of contig sequence
        sequence - string - sequence of the contig
        description - string - Description of the contig (e.g. everything after the ID in a FASTA file)

    @optional length md5 genetic_code cell_compartment replicon_geometry replicon_type name description complete
    */
    typedef structure {
        Contig_id id;
        int length;
        string md5;
        string sequence;/*using "sequence" instead of "dna"*/
        int genetic_code;
        string cell_compartment;
        string replicon_type;
        /* circular / linear */
        string replicon_geometry;
        string name;
        string description;
        Bool complete;
    } Contig;

    /*
    Type spec for the "ContigSet" object

    Fields:
        id - string - unique kbase ID of the contig set
        name - string - name of the contig set
        type - string - type of the contig set (values are: Genome,Transcripts,Environment,Collection)
        source_id - string - source ID of the contig set
        source - string - source of the contig set
        contigs - list<Contig> - list of contigs in the contig set
        reads_ref - string - reference to the shocknode with the raw reads from which contigs
            were assembled
        fasta_ref - string - reference to fasta file from which contig set were read

    @optional name type reads_ref fasta_ref
    @metadata ws type as Type
    @metadata ws source_id as Source ID
    @metadata ws source as Source
    @metadata ws name as Name
    @metadata ws length(contigs) as Number contigs
    */
    typedef structure {
        ContigSet_id id;
        string name;
        string md5;
        source_id source_id;
        string source;
        string type;
        Reads_ref reads_ref;
        Fasta_ref fasta_ref;
        list<Contig> contigs;
    } ContigSet;

    /*
    Structure for a publication

    Elements:
        (0) pubmedid - float
        (1) source - string - (ex. Pubmed)
        (2) title - string
        (3) string web address - string
        (4) publication year - string
        (5) authors - string
        (6) journal - string
    */
    typedef tuple<float pubmedid,string source,string title, string url,string year,string authors, string journal> publication;

    /*
    KBase CDS ID
    @id external
    */
    typedef string cds_id;

    /*
    KBase mRNA ID
    @id external
    */
    typedef string mrna_id;

    /*
    Type spec for the "InferenceInfo" object.
    TODO docs
    Found in the `inference_data` fields in mRNAs and CDSs

    Fields:
        category - string - TODO
        type - string - TODO
        evidence - string - TODO
    */
    typedef structure {
        string category;
        string type;
        string evidence;
    } InferenceInfo;

    /*
    Structure for a single CDS encoding “gene” of a genome
    ONLY PUT GENES THAT HAVE A CORRESPONDING CDS IN THIS ARRAY

    NOTE: Sequence is optional. Ideally we can keep it in here, but
    Recognize due to space constraints another solution may be needed.
    We may want to add additional fields for other CDM functions
    (e.g., atomic regulons, coexpressed fids, co_occurring fids,...)

    protein_translation_length and protein_translation are
    for longest coded protein (representative protein for splice variants)

    NOTE: New Aliases field definitely breaks compatibility.
          As Does Function.

    flags are flag fields in GenBank format. This will be a controlled vocabulary.
    Initially Acceptable values are pseudo, ribosomal_slippage, and trans_splicing

    Md5 is the md5 of dna_sequence.

    @optional functions ontology_terms note protein_translation mrnas flags warnings
    @optional inference_data dna_sequence aliases db_xrefs children functional_descriptions
    */
    typedef structure {
        Feature_id id;
        list<tuple<Contig_id,int,string,int>> location;
        list<string> functions;
        list<string> functional_descriptions;
        mapping<string ontology_namespace,mapping<string ontology_id,list<int> evidence_events>> ontology_terms;
        string note;
        string md5;
        string protein_translation;
        int protein_translation_length;
        list<string> cdss;
        list<string> mrnas;
        list<string> children;
        list<string> flags;
        list<string> warnings;
        list <InferenceInfo> inference_data;
        string dna_sequence;
        int dna_sequence_length;
        list<tuple<string fieldname,string alias>> aliases;
        list<tuple<string db_source,string db_identifier>> db_xrefs;
    } Feature;

    /*
    Structure for a single feature that is NOT one of the following:
     - Protein encoding gene (gene that has a corresponding CDS)
     - mRNA
     - CDS

    Note pseudo-genes and Non protein encoding genes are put into this

    flags are flag fields in GenBank format. This will be a controlled vocabulary.
    Initially Acceptable values are pseudo, ribosomal_slippage, and trans_splicing
    Md5 is the md5 of dna_sequence.

    @optional functions ontology_terms note flags warnings functional_descriptions
    @optional inference_data dna_sequence aliases db_xrefs children parent_gene
    */
    typedef structure {
        Feature_id id;
        list<tuple<Contig_id,int,string,int>> location;
        string type;
        list<string> functions;
        list<string> functional_descriptions;
        mapping<string ontology_namespace,mapping<string ontology_id,list<int> evidence_event>> ontology_terms;
        string note;
        string md5;
        string parent_gene;
        list<string> children;
        list<string> flags;
        list<string> warnings;
        list <InferenceInfo> inference_data;
        string dna_sequence;
        int dna_sequence_length;
        list<tuple<string fieldname,string alias>> aliases;
        list<tuple<string db_source,string db_identifier>> db_xrefs;
    } NonCodingFeature;

    /*
    Structure for a single coding sequence.

    Coding sequences are the sections of a feature's sequence that are translated
    to a protein (minus introns and UTRs).

    Fields:
        id - string - identifier of the coding sequence, such as "b0001_CDS_1"
        location - list<tuple<string, int, string, int>> - list of
            locations from where this sequence originates in the original assembly.
            Each sub-sequence in the list constitutes a section of the resulting
            CDS. The first element in the tuple corresponds to the "contig_id",
            such as "NC_000913.3". The second element in the tuple is an index in
            the contig of where the sequence starts. The third element is either a
            plus or minus sign indicating whether it is on the 5' to 3' leading
            strand ("+") or on the 3' to 5' lagging strand ("-"). The last element
            is the length of the sub-sequence.
            For a location on the leading strand (denoted by "+"), the index is
            of the leftmost base, and the sequence extends to the right. For a
            location on the lagging strand (denoted by "-"), the index is of
            the rightmost base, and the sequence extends to the left.
            NOTE: the last element in each tuple is the *length* of each
            sub-sequence. If you have a location such as ("xyz", 100, "+", 50),
            then your sequence will go from index 100 to index 149 (this has a
            length of 50). It *does not* go from index 100 to index 150, as
            that would have a length of 51.
            Likewise, if you have the location ("xyz", 100, "-", 50), then the
            sequence extends from 100 down to 51, which has a length of 50
            bases. It does not go from index 100 to 50, as that would have a
            length of 51.
        md5 - string - md5 of the dna sequence - TODO clarification
        protein_md5 - string - hash of the protein sequence that this CDS encodes
        parent_gene - string - gene (feature) from which this CDS comes from,
            including introns and UTRs that have been removed to create this CDS.
        parent_mrna - string - mRNA sequence from which this sequence is derived,
            including UTRs but not introns.
        note - string - TODO
        functions - list<string> - list of protein products or chemical
            processes that this sequence creates, facilitates, or influences.
        functional_descriptions - list<string> - TODO list of protein products or chemical
            processes that sequence creates, facilitates, or influences.
        ontology_terms - mapping<string, mapping<string, list<int>>> - a mapping
            of ontology source id (eg. "GO") to a mapping of term IDs (eg "GO:16209")
            to a list of indexes into the ontology_events data (found in the top
            level of the genome object). The index into an ontology event indicates
            what service and method created this term assignment.
        flags - list<string>  - (controlled vocab) fields from the genbank source. A
            common example is "pseudo" for pseudo-genes that do not encode proteins,
            which shows up as "/pseudo" in the genbank.
            Values can be: "pseudo", "ribosomal_slippage", "trans_splicing"
        warnings - list<string> - list of plain text warnings related to importing or constructing
            each feature
        inference_data - list<InferenceInfo> - TODO
        protein_translation - string - amino acid sequence that this CDS gets translated into.
        protein_translation_length - int - length of the above
        aliases - list<(string, string)> - alternative list of names or identifiers
            eg: [["gene", "thrA"], ["locus_tag", "b0002"]]
        db_xrefs - list<(string, string)> - Identifiers from other databases (database cross-references)
            The first string is the database name, the second is the database identifier.
            eg: [["ASAP", "ABE-0000006"], ["EcoGene", "EG11277"]]
        dna_sequence - string - sequence of exons from the genome that constitute this protein encoding sequence.
        dna_sequence_length - int - length of the above

    @optional parent_gene parent_mrna functions ontology_terms note flags warnings
    @optional inference_data dna_sequence aliases db_xrefs functional_descriptions
    */
    typedef structure {
        cds_id id;
        list<tuple<Contig_id, int, string, int>> location;
        string md5;
        string protein_md5;
        Feature_id parent_gene;
        mrna_id parent_mrna;
        string note;
        list<string> functions;
        list<string> functional_descriptions;
        mapping<string ontology_namespace, mapping<string ontology_id, list<int> evidence_events>> ontology_terms;
        list<string> flags;
        list<string> warnings;
        list<InferenceInfo> inference_data;
        string protein_translation;
        int protein_translation_length;
        list<tuple<string fieldname, string alias>> aliases;
        list<tuple<string db_source, string db_identifier>> db_xrefs;
        string dna_sequence;
        int dna_sequence_length;
    } CDS;

    /*
    The mRNA is the transcribed sequence from the original feature, minus the
    introns, but including the UTRs.

    Fields:
        id - string - identifying string for the mRNA
        location - list<tuple<string, int, string, int>> - list of
            locations from where this sequence originates in the original assembly.
            Each sub-sequence in the list constitutes a section of the resulting
            CDS. The first element in the tuple corresponds to the "contig_id",
            such as "NC_000913.3". The second element in the tuple is an index in
            the contig of where the sequence starts. The third element is either a
            plus or minus sign indicating whether it is on the 5' to 3' leading
            strand ("+") or on the 3' to 5' lagging strand ("-"). The last element
            is the length of the sub-sequence.
            For a location on the leading strand (denoted by "+"), the index is
            of the leftmost base, and the sequence extends to the right. For a
            location on the lagging strand (denoted by "-"), the index is of
            the rightmost base, and the sequence extends to the left.
            NOTE: the last element in each tuple is the *length* of each
            sub-sequence. If you have a location such as ("xyz", 100, "+", 50),
            then your sequence will go from index 100 to index 149 (this has a
            length of 50). It *does not* go from index 100 to index 150, as
            that would have a length of 51.
            Likewise, if you have the location ("xyz", 100, "-", 50), then the
            sequence extends from 100 down to 51, which has a length of 50
            bases. It does not go from index 100 to 50, as that would have a
            length of 51.
        md5 - string - md5 of the dna sequence used to check equality of genomes
        parent_gene - Feature_id - corresponding feature for this sequence, including introns and UTRs
        cds - string - corresponding coding sequence for this mRNA (the sequence minus UTRs)
        dna_sequence - string - sequence of UTRs and exons from the genome that constitute this mRNA
        dna_sequence_length - int - length of the above
        note - string - TODO
        functions - list<string> - TODO list of protein products or chemical
            processes that sequence creates, facilitates, or influences.
        functional_descriptions - list<string> - TODO list of protein products or chemical
            processes that sequence creates, facilitates, or influences.
        ontology_terms - mapping<string, mapping<string, list<int>>> - a mapping
            of ontology source id (eg. "GO") to a mapping of term IDs (eg "GO:16209")
            to a list of indexes into the ontology_events data (found in the top
            level of the genome object). The index into an ontology event indicates
            what service and method created this term assignment.
        flags - list<string> - controlled vocab - fields from the genbank source. A
            common example is "pseudo" for pseudo-genes that do not encode proteins,
            which shows up as "/pseudo" in the genbank.
            Values can be: "pseudo", "ribosomal_slippage", "trans_splicing"
        warnings - list<string> - TODO
        inference_data - list<InferenceInfo> - TODO
        aliases - list<(string, string)> - alternative list of names or identifiers
            eg: [["gene", "thrA"], ["locus_tag", "b0002"]]
        db_xrefs - list<(string, string)> - Identifiers from other databases (database cross-references).
            The first string is the database name, the second is the database identifier.
            eg: [["ASAP", "ABE-0000006"], ["EcoGene", "EG11277"]]

    @optional parent_gene cds functions ontology_terms note flags warnings
    @optional inference_data dna_sequence aliases db_xrefs functional_descriptions
    */
    typedef structure {
        mrna_id id;
        list<tuple<Contig_id, int, string, int>> location;
        string md5;
        Feature_id parent_gene;
        cds_id cds;
        string dna_sequence;
        int dna_sequence_length;
        string note;
        list<string> functions;
        list<string> functional_descriptions;
        mapping<string ontology_namespace, mapping<string ontology_id, list<int> evidence_events>> ontology_terms;
        list<string> flags;
        list<string> warnings;
        list<InferenceInfo> inference_data;
        list<tuple<string fieldname, string alias>> aliases;
        list<tuple<string db_source, string db_identifier>> db_xrefs;
    } mRNA;

    /*
    Reference to a taxon object
    @id ws KBaseGenomeAnnotations.Taxon
    */
    typedef string Taxon_ref;

    /*
    Reference to a handle to the Genbank file on shock
    @id handle
    */
    typedef string genbank_handle_ref;

    /*
    Reference to a handle to the GFF file on shock
    @id handle
    */
    typedef string gff_handle_ref;

    /*
    Reference to a ontology object
    @id ws KBaseOntology.OntologyDictionary
    */
    typedef string Ontology_ref;

    /*
    Reference to a report object
    @id ws KBaseReport.Report
    */
    typedef string Method_report_ref;

    /*
    @optional ontology_ref method_version eco description
    */
    typedef structure {
        string id;
        Ontology_ref ontology_ref;
        string method;
        string method_version;
        string timestamp;
        string eco;
        string description;
    } Ontology_event;

    /*
    Genome quality score

    Fields:
        method - string - TODO
        method_report_ref - string - TODO
        method_version - string - TODO
        score: string - TODO
        score_interpretation - string - TODO
        timestamp - string - TODO

    Score_interpretation - fraction_complete - controlled vocabulary managed by API
    @optional method_report_ref method_version
    */
    typedef structure {
        string method;
        Method_report_ref method_report_ref;
        string method_version;
        string score;
        string score_interpretation;
        string timestamp;
    } GenomeQualityScore;

    /*
    Lineage information for a genome.

    lineage - the lineage string from the data source. For example, for GTDB:
        d__Bacteria;p__Proteobacteria;c__Gammaproteobacteria;o__Enterobacterales;f__Enterobacteriaceae;g__Escherichia;s__Escherichia coli
        For GTDB, if the lineage is not fully resolved, enter the string in GTDB style
        including the unresolved ranks, e.g.
        d__Bacteria;p__Proteobacteria;c__Gammaproteobacteria;o__Enterobacterales;f__;g__;s__
    source_ver - the version of the source data. For example, for GTDB: 207.0
    taxon_id - the ID of the taxon to which the genome belongs. For example, for GTDB:
        f__Enterobacteriaceae. Note that while ideally this will be a leaf in the taxonomy tree,
        that is not necessarily the case and the taxon_id may be an internal node.
    source_id - the ID of the genome at the source. For example, for GTDB: RS_GCF_000566285.1.
        In some cases this may be the same as the taxon_id. Expected to be present for genomes
        that are part of the data set, but not for genomes that are inserted into the
        taxonomy tree (by gtdb_tk, for instance).

    @optional source_id
    */
    typedef structure {
        string lineage;
        string source_ver;
        string taxon_id;
        string source_id;
    } Lineage;

    /*
        Standard lineage providers - NCBI and GTDB.

        More standard lineage providers may be added here as necessary.

        @optional ncbi gtdb
    */
    typedef structure {
        Lineage ncbi;
        Lineage gtdb;
    } StandardLineages;

    /*
    Genome type -- annotated and assembled genome data.

    Field descriptions:
        id - string - KBase legacy data ID
        scientific_name - string - human readable species name
        domain - string - human readable phylogenetic domain name (eg. "Bacteria")
        warnings - list of string - genome-level warnings generated in the annotation process
        genome_tiers - list of string - controlled vocabulary (based on app input and checked by GenomeFileUtil)
            A list of labels describing the data source for this genome.
            Allowed values - Representative, Reference, ExternalDB, User
            Tier assignments based on genome source:
             * All phytozome - Representative and ExternalDB
             * Phytozome flagship genomes - Reference, Representative and ExternalDB
             * Ensembl - Representative and ExternalDB
             * RefSeq Reference - Reference, Representative and ExternalDB
             * RefSeq Representative - Representative and ExternalDB
             * RefSeq Latest or All Assemblies folder - ExternalDB
             * User Data - User tagged
        feature_counts - map of string to integer - total counts of each type of feature
            keys are a controlled vocabulary of - "CDS", "gene", "misc_feature",
            "misc_recomb", "mobile_element", "ncRNA" - 72, "non_coding_features",
            "non_coding_genes", "protein_encoding_gene", "rRNA", "rep_origin",
            "repeat_region", "tRNA"
        genetic_code - int - An NCBI-assigned taxonomic category for the organism
            See here - https://www.ncbi.nlm.nih.gov/Taxonomy/Utils/wprintgc.cgi
        dna_size - integer - total number of nucleotides
        num_contigs - integer - total number of contigs in the genome
        molecule_type - string - controlled vocab - the type of molecule sequenced
            Possible values are "Unknown", "DNA", "RNA", "genomic DNA", "genomic RNA",
            "mRNA", "tRNA", "rRNA", "other RNA", "other DNA", "transcribed RNA",
            "viral cRNA", "unassigned DNA", "unassigned RNA"
        contig_lengths - list of int - nucleotide length of each contig in the genome
            Indexes in this list correspond to indexes in the `contig_ids` list.
        contig_ids - list of str - external database identifiers for each contig (eg. "NC_000913.3")
        source - str - controlled vocab - descriptor of where this data came from (eg. "RefSeq")
            Allowed entries RefSeq, Ensembl, Phytozome, RAST, Prokka, User_upload
        source_id - string - identifier of this genome from the source database (eg. the RefSeq ID such as "NC_000913")
        md5 - string - checksum of the underlying assembly sequence
        taxonomy - string - semicolon-delimited taxonomy lineage, in order of parent to child
        taxon_assignments - mapping of taxonomy namespace to taxon ID.
            example - {"ncbi": "286", "gtdb": "s__staphylococcus_devriesei"}
        std_lineages - similar to taxon_assignments; contains lineage information for standard
            lineages like NCBI and GTDB. Implemented as a struct rather than a mapping so that
            the metadata annotation may be applied to the fields.
        gc_content - float - ratio of GC count to AT in the genome
        publications - tuple of (pubmedid, source, title, web_addr, year, authors, journal). See typedef above.
        ontology_events - A record of the service and method used for a set of
            ontology assignments on the genome.
        ontologies_present - a mapping of ontology source id (eg. "GO") to a mapping
            of term IDs (eg "GO:16209") to term names (eg. "histidine biosynthetic process").
        features - array of Feature - protein coding genes (see the separate Feature spec)
        cdss - array of protein-coding sequences
        mrnas - array of transcribed messenger RNA sequences (equal to cdss plus 5' and 3' UTRs)
        non_coding_features - array of features that does not include mRNA, CDS, and protein-encoding genes
        assembly_ref - workspace reference to an assembly object from which this annotated genome was derived.
        taxon_ref - workspace reference to a taxon object that classifies the species or strain of this genome.
        genbank_handle_ref - file server handle reference to the source genbank file for this genome.
        gff_handle_ref - file server handle reference to the source GFF file for this genome.
        external_source_origination_date - TODO look at GFU for this
        release - string - User-supplied release or version of the source data. This
            most likely will come from an input field in the import app.
        original_source_file_name - filename from which this genome was derived (eg. genbank or gff filename).
        notes - TODO
        quality_scores - TODO
        suspect - bool - flag of whether this annotation is problematic due to some warning
        genome_type - string - controlled vocab - One of "draft isolate",
            "finished isolate", "mag", "sag", "virus", "plasmid", "construct"

    Features vs. coding sequences: a feature is a sequence in the DNA that codes
    for a protein, including non-transcribed introns. A coding sequence (stored as
    `cdss`) includes **only** the sections of the feature that codes for a protein,
    minus introns and UTRs.

    @optional warnings contig_lengths contig_ids source_id taxonomy publications
    @optional ontology_events ontologies_present non_coding_features mrnas genome_type
    @optional genbank_handle_ref gff_handle_ref external_source_origination_date
    @optional release original_source_file_name notes quality_scores suspect assembly_ref
    @optional taxon_ref taxon_assignments std_lineages

    @metadata ws gc_content as GC content
    @metadata ws taxonomy as Taxonomy
    @metadata ws md5 as MD5
    @metadata ws dna_size as Size
    @metadata ws genetic_code as Genetic code
    @metadata ws domain as Domain
    @metadata ws source_id as Source ID
    @metadata ws source as Source
    @metadata ws scientific_name as Name
    @metadata ws genome_type as Genome Type
    @metadata ws length(features) as Number of Protein Encoding Genes
    @metadata ws length(cdss) as Number of CDS
    @metadata ws assembly_ref as Assembly Object
    @metadata ws num_contigs as Number contigs
    @metadata ws length(warnings) as Number of Genome Level Warnings
    @metadata ws suspect as Suspect Genome
    @metadata ws std_lineages.ncbi.lineage as NCBI_lineage
    @metadata ws std_lineages.ncbi.source_ver as NCBI_source_ver
    @metadata ws std_lineages.ncbi.taxon_id as NCBI_taxon_id
    @metadata ws std_lineages.ncbi.source_id as NCBI_source_id
    @metadata ws std_lineages.gtdb.lineage as GTDB_lineage
    @metadata ws std_lineages.gtdb.source_ver as GTDB_source_ver
    @metadata ws std_lineages.gtdb.taxon_id as GTDB_taxon_id
    @metadata ws std_lineages.gtdb.source_id as GTDB_source_id
    */
    typedef structure {
        Genome_id id;
        string scientific_name;
        string domain;
        list<string> warnings;
        list<string> genome_tiers;
        mapping<string type, int count> feature_counts;
        int genetic_code;
        int dna_size;
        int num_contigs;
        string molecule_type;
        list<int> contig_lengths;
        list<string> contig_ids;
        string source;
        source_id source_id;
        string md5;
        string taxonomy;
        StandardLineages std_lineages;
        mapping<string, string> taxon_assignments;
        float gc_content;
        list<publication> publications;
        list<Ontology_event> ontology_events;
        mapping<string ontology_namespace, mapping<string ontology_id, string termname>> ontologies_present;
        list<Feature> features;
        list<NonCodingFeature> non_coding_features;
        list<CDS> cdss;
        list<mRNA> mrnas;
        Assembly_ref assembly_ref;
        Taxon_ref taxon_ref;
        genbank_handle_ref genbank_handle_ref;
        gff_handle_ref gff_handle_ref;
        string external_source_origination_date;
        string release;
        string original_source_file_name;
        string notes;
        list<GenomeQualityScore> quality_scores;
        Bool suspect;
        string genome_type;
    } Genome;

    /*
    Structure for a protein family

    @optional query_begin query_end subject_begin subject_end score evalue subject_description release_version
     */
    typedef structure {
        string id;
        string subject_db;
        string release_version;
        string subject_description;
        int query_begin;
        int query_end;
        int subject_begin;
        int subject_end;
        float score;
        float evalue;
    } ProteinFamily;

    /* TODO docs */
    typedef tuple<string comment, string annotator, float annotation_time> annotation;

    /*
    Type spec for the "Protein" object

    Fields:
        id - string - unique external ID of protein
        function - string - annotated function for protein
        md5 - string - md5 hash of protein sequence
        sequence - string - amino acid sequence of protein
        length - int -length of protein
        protein_families - list<ProteinFamily> - families to which the protein belongs
        aliases - list<string> - aliases for the protein
        annotations - list<annotation> - curator annotations on protein
        subsystem_data - list<subsystem_data> - TODO

    @optional function
    @searchable ws_subset id md5 function length aliases
    */
    typedef structure {
        Protein_id id;
        string function;
        string md5;
        string sequence;
        int length;
        list<ProteinFamily> protein_families;
        list<string> aliases;
        list<annotation> annotations;
    } Protein;

    /*
    Type spec for the "ProteinSet" object

    Fields:
        id - string - unique kbase ID of the protein set
        name - string - name of the protein set
        type - string - type of the protein set (values are: Organism,Environment,Collection)
        source_id - string - source ID of the protein set
        source - string -source of the protein set
        proteins - list<Protein> - list of proteins in the protein set
        fasta_ref - fasta_ref - reference to fasta file from which contig set were read

    @optional name type fasta_ref
    @searchable ws_subset proteins.[*].(id,md5,function,length,aliases) md5 id name source_id source type
    */
    typedef structure {
        ProteinSet_id id;
        string name;
        string md5;
        source_id source_id;
        string source;
        string type;
        Fasta_ref fasta_ref;
        list<Protein> proteins;
    } ProteinSet;

    /*
    A function_probability is a (annotation, probability) pair associated with a gene
    An annotation is a "///"-delimited list of roles that could be associated with that gene.
    */
    typedef tuple<string annotation, float probability> function_probability;

    /*
    Object to carry alternative functions and probabilities for genes in a genome

    Fields:
        id - string - ID of the probabilistic annotation object
        genome_ref - string - reference to genome probabilistic annotation was built for
        roleset_probabilities - mapping<string, list<function_probability>> - mapping of
            features to list of alternative function_probability objects
        skipped_features - list<string> - list of features in genome with no probability

    @searchable ws_subset id genome_ref skipped_features
    */
    typedef structure {
        ProbabilisticAnnotation_id id;
        Genome_ref genome_ref;
        mapping<Feature_id,list<function_probability>> roleset_probabilities;
        list<Feature_id> skipped_features;
    } ProbabilisticAnnotation;

    /*
    Structure for the "MetagenomeAnnotationOTUFunction" object

    Fields:
        reference_genes - list<string> - list of genes associated with hit
        functional_role - string - annotated function
        kbid - string - kbase ID of OTU function in metagenome
        abundance - int - number of hits with associated role and OTU
        confidence - float - confidence of functional role hit
        confidence_type - string - type of functional role hit

    @searchable ws_subset id abundance confidence functional_role
    */
    typedef structure {
        string id;
        list<string> reference_genes;
        string functional_role;
        int abundance;
        float confidence;
    } MetagenomeAnnotationOTUFunction;

    /*
    Structure for the "MetagenomeAnnotationOTU" object

    Fields:
        name - string - name of metagenome OTU
        kbid - string - KBase ID of OTU of metagenome object
        source_id - string - ID used for OTU in metagenome source
        source - string - source OTU ID
        functions - list<MetagenomeAnnotationOTUFunction> - list of functions in OTU

    @searchable ws_subset id name source_id source functions.[*].(id,abundance,confidence,functional_role)
    */
    typedef structure {
        float ave_confidence;
        float ave_coverage;
        string id;
        string name;
        string source_id;
        string source;
        list<MetagenomeAnnotationOTUFunction> functions;
    } MetagenomeAnnotationOTU;

    /*
    Structure for the "MetagenomeAnnotation" object

    Fields:
        type - string - type of metagenome object
        name - string - name of metagenome object
        kbid - string - KBase ID of metagenome object
        source_id - string - ID used in metagenome source
        source - string - source of metagenome data
        confidence_type - string - type of confidence score
        otus - list<MetagenomeAnnotationOTU> - list of otus in metagenome

    @searchable ws_subset type name id source_id source confidence_type otus.[*].(id,name,source_id,source,functions.[*].(id,abundance,confidence,functional_role))
    @metadata ws type as Type
    @metadata ws name as Name
    @metadata ws source_id as Source ID
    @metadata ws source as Source
    @metadata ws length(otus) as Number OTUs
    */
    typedef structure {
        string type;
        string name;
        string id;
        string source_id;
        string source;
        string confidence_type;
        list<MetagenomeAnnotationOTU> otus;
    } MetagenomeAnnotation;

    /*
    Domain - a subobject holding information on a single protein domain

    Fields:
        id - string - numerical ID assigned by KBase
        source_id - string - assession ID from CDD database;
        type - string - type of CDD, possible values are cd, pfam, smart, COG, PRK, CHL
        name - string - name of CDD
        description - string - description of CDD
    */
    typedef structure {
        string id;
        string source_id;
        string type;
        string name;
        string description;
    } Domain;

    /*
    FeatureDomain - a subobject holding information on how a domain appears in a gene

    Fields:
        id - string - numerical ID assigned by KBase
        source_id - string - assession ID from CDD database;
        type - string - type of CDD, possible values are cd, pfam, smart, COG, PRK, CHL
        name - string - name of CDD
        description - string - description of CDD

    @optional feature_ref domains
    */
    typedef structure {
        string id;
        string feature_id;
        string feature_ref;
        string function;
        int feature_length;
        list<tuple<string domain_ref,int identity,int alignment_length,int mismatches,int gaps,float protein_start,float protein_end,float domain_start,float domain_end,float evalue,float bit_score>> domains;
    } FeatureDomainData;

    /*
    GenomeDomainData object: this object holds all data regarding protein domains in a genome in
    KBase

    @optional genome_ref
    @searchable ws_subset id genome_id scientific_name genome_ref num_domains num_features
    */
    typedef structure {
        string id;
        Genome_id genome_id;
        string scientific_name;
        Genome_ref genome_ref;
        int num_domains;
        int num_features;
        list<Domain> domains;
        list<FeatureDomainData> featuredomains;
    } GenomeDomainData;


    /*
    OrthologFamily object: this object holds all data for a single ortholog family in a metagenome.

    Fields:
    id - string - group identifier
    type - string - ... (may duplicate "cat"? leave alone for backwards)
    gene_name - list<string> - name as described in KBaseGenomes.Genome
    function - string - function as described in KBaseGenomes.Genome
    function_sources - list<tuple<string,string>> - list of tuples of:
        (0) string - gene identifier (ID in gff file)
        (1) string - genome workspace reference
    function_logic - string - controlled vocab of:
        (1) 'single_source'
        (2) 'union'
        (3) 'intersection'
    md5 - string - md5 encoded string of protein_translation
    protein_translation - string - protein translation string
    protein_translation_source - tuple<string,string> - tuple of:
        (0) string - gene identifier (ID in gff file)
        (1) string - genome workspace reference
    orthologs - list<tuple<string,float,string>> - list of tuples of:
        (0) string - gene identifier (ID in gff file)
        (1) float - numerical order in gff file OR gene order in BLAST
        (2) string - genome workspace reference
    genome_occ - int - number of input genomes in cluster
    cat - string - controlled vocabulary of cluster classification -
        depends on method (see Pangenome struct)
    core_log_likelihood - float - mOTUpan-specific score
    mean_copies - float - avg number of copies per genome with cluster

    @optional type gene_name function function_sources function_logic md5 protein_translation protein_translation_source genome_occ cat core_log_likelihood mean_copies
    */
    typedef structure {
	string id;
	string type;
	list<string> gene_name;
	string function;
	list<tuple<string,string>> function_sources;
	string function_logic;
	string md5;
	string protein_translation;
	tuple<string,string> protein_translation_source;
	list<tuple<string, float, string>> orthologs;
	int genome_occ;
	string cat;
	float core_log_likelihood;
	float mean_copies;
    } OrthologFamily;


    /*
    Pangenome object: this object holds all data regarding a pangenome

    @searchable ws_subset id name
    @metadata ws type as Type
    @metadata ws name as Name
    @metadata ws length(orthologs) as Number orthologs
    @metadata ws length(genome_refs) as Number genomes

    Fields:
    id - string - pangenome identifier
    name - string - pangenome name
    type - string - method used to calculate pangenome -
        controlled vocab:
        (1) 'OrthoMCL'
        (2) 'mOTUpan'
    type_ver - string - version of pangenome calc method
    cluster_cats - mapping<string,mapping<string,string>> -
        controlled vocab keyed by pg calc type to standard terms
        current standard terms: 'core','flexible','singleton'
        e.g.:
        cluster_cats = { 'mOTUpan': { 'core': 'core',
                                      'accessory': 'flexible'
                                    },
                       { 'OrthoMCL': { 'core': 'core',
                                       'peripheral': 'flexible',
                                       'singleton': 'singleton'
                                     }
                       }
    clustering_method - method for clustering genes -
        controlled vocab:
        (1) OrthoMCL
        (2) MMseqs2
    clustering_method_ver - string - version of clustering method
    clustering_method_params - mapping<string,string> - clust params
    pangenome_method_params - mapping<string,string> - pg calc params
    genome_count - int - number of input genomes
    core_length - int - number of clusters considered 'core'
    mean_est_genome_size - float - estimate of avg genes per genome
    prior_genome_completeness - mapping<Genome_name,float> -
        percentage value dict with genome_ref as key
    posterior_genome_completeness - mapping<Genome_name,float> -
        percentage value dict with genome_ref as key
    genome_refs - list<string> - input genome refs
    genome_names - list<string> - input genome names
    genome_ref_to_name - mapping<Genome_ref,Genome_name> - lookup dict
    genome_name_to_ref - mapping<Genome_name,Genome_ref> - lookup dict
    orthologs - list<OrthologFamily> - pangenome clusters

    @optional type_ver cluster_cats clustering_method clustering_method_ver clustering_method_params pangenome_method_params genome_count core_length mean_est_genome_size prior_genome_completeness posterior_genome_completeness genome_names genome_ref_to_name genome_name_to_ref
    */
    typedef structure {
	string id;
	string name;
	string type;
	string type_ver;
	mapping<string,mapping<string,string>> cluster_cats;
	string clustering_method;
	string clustering_method_ver;
	mapping <string,string> clustering_method_params;
	mapping <string,string> pangenome_method_params;
	int genome_count;
	int core_length;
	float mean_est_genome_size;
	mapping<Genome_name,float> prior_genome_completeness;
	mapping<Genome_name,float> posterior_genome_completeness;
	list<Genome_ref> genome_refs;
	list<Genome_name> genome_names;
	mapping<Genome_ref,Genome_name> genome_ref_to_name;
	mapping<Genome_name,Genome_ref> genome_name_to_ref;
	list<OrthologFamily> orthologs;
    } Pangenome;


    /*
    GenomeComparisonGenome object: this object holds information about a genome in a genome
    comparison.
    */
    typedef structure {
        string id;
        Genome_ref genome_ref;
        mapping<string genome_id,tuple<int commonfamilies,int commonfunctions> > genome_similarity;
        string name;
        string taxonomy;
        int features;
        int families;
        int functions;
    } GenomeComparisonGenome;

    /*
    GenomeComparisonFunction object: this object holds information about a genome in a function
    across all genomes.
    */
    typedef structure {
        int core;
        mapping<string genome_id,list<tuple<Feature_id,int famindex,float score> > > genome_features;
        string id;
        list<tuple<Reaction_id, string equation>> reactions;
        string subsystem;
        string primclass;
        string subclass;
        int number_genomes;
        float fraction_genomes;
        float fraction_consistent_families;
        string most_consistent_family;
    } GenomeComparisonFunction;

    /*
    GenomeComparisonFamily object: this object holds information about a protein family across a set
    of genomes.
    */
    typedef structure {
        int core;
        mapping<string genome_id,list< tuple<Feature_id,list<int> funcindecies,float score > > > genome_features;
        string id;
        string type;
        string protein_translation;
        int number_genomes;
        float fraction_genomes;
        float fraction_consistent_annotations;
        string most_consistent_role;
    } GenomeComparisonFamily;

    /*
    GenomeComparisonData object: this object holds information about a multigenome comparison.

    @optional protcomp_ref pangenome_ref
    @metadata ws core_functions as Core functions
    @metadata ws core_families as Core families
    @metadata ws name as Name
    @metadata ws length(genomes) as Number genomes
    */
    typedef structure {
        string id;
        string name;
        int core_functions;
        int core_families;
        Protcomp_ref protcomp_ref;
        Pangenome_ref pangenome_ref;
        list<GenomeComparisonGenome> genomes;
        list<GenomeComparisonFamily> families;
        list<GenomeComparisonFunction> functions;
    } GenomeComparison;

    /*
    Reference to a Genome object in the workspace
    @id ws KBaseCollections.GenomeSet
    */
    typedef string GenomeSet_ref;

    /*
    Reference to a handle to the tables in SQLLite format
    @id handle
    */
    typedef string table_handle_ref;

    /*
    Data structure for storing pangenome data
    */
    typedef structure {
        string pangenome_id;
        string pangenome_taxonomy;
        list<Genome_ref> user_genomes;
        list<string> datalake_genomes;
        table_handle_ref sqllite_tables_handle_ref;
    } PangenomeData;

    /*
    GenomeDataLakeTables object: this object holds the DataLake tables with contextual data for the input genome set

    @metadata ws genomeset_ref as Genome set
    @metadata ws description as Description
    @metadata ws name as Name
    @metadata ws length(pangenome_data) as Number pangenomes
    */
    typedef structure {
        string description;
        string name;
        GenomeSet_ref genomeset_ref;
        list<PangenomeData> pangenome_data;
    } GenomeDataLakeTables;
};
