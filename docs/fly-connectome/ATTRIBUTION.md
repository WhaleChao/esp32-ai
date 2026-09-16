# Connectome data attribution

The graph is derived from **MaleCNS v1.0**, the male Drosophila melanogaster
central nervous system connectome, provided by the FlyEM Project at HHMI Janelia
Research Campus, University of Cambridge Department of Zoology, MRC Laboratory
of Molecular Biology and Google Research.

- [Official project and dataset license](https://male-cns.janelia.org/)
- [Versioned bulk downloads](https://male-cns.janelia.org/download/)
- [Creative Commons Attribution 4.0 International](https://creativecommons.org/licenses/by/4.0/)
- [Full license text](LICENSE-DATA)

Citation: Stuart Berg, Isabella R. Beckett, Marta Costa, Philipp Schlegel,
Michał Januszewski, et al. *Sexual dimorphism in the complete Drosophila male
central nervous system connectome*. Cell 189(18), 5504–5526.e15 (2026).
[DOI: 10.1016/j.cell.2026.08.015](https://doi.org/10.1016/j.cell.2026.08.015).

[source-data.json](source-data.json) identifies the upstream files and their
hashes. Annotation superclasses select cells; the connection-strength table
supplies positive integer contact counts. The other source files listed there
were used during exploration and are not signed weights or geometry in the runtime.

Viacheslav Sierbov / slvDev selected an induced subgraph by cell superclass,
reordered local node indices and compressed the graph into independent LZ4
blocks. Every retained source/destination pair and integer contact count is
preserved. [subgraph.json](subgraph.json) records the selection and exclusions.
The model package supplies `neurons.csv` to map execution indices to source body IDs.

The rate dynamics, Q29 arithmetic, engineered inputs and the escape demonstration
are additions by this project. They are not biological behavior measurements
or claims made by the upstream authors. This project is not endorsed by them.

The derived model/data package uses CC BY 4.0. Preserve attribution, license
information and an account of modifications when redistributing it. The
independently authored runtime, firmware and analysis tools use this repository's
MIT license. The MIT license does not relicense the upstream dataset.
