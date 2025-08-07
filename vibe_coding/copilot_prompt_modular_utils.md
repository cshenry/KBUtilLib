# GitHub Copilot Agent Prompt: Modular Utility Framework Setup

This prompt is intended for use **after scaffolding a project using a custom Cookiecutter template**. It guides GitHub Copilot (or another VS Code agent) to help implement the internal architecture of a flexible, modular Python utility framework.

---

## 🧠 Prompt for Copilot Agent (Post-Cookiecutter Setup)

> I’ve already scaffolded this project using a custom Cookiecutter template. The boilerplate structure is in place, including modern packaging, configuration, and documentation tools. Now I want to build out the internal modular architecture and migrate existing utility code into this new system.
>
> ### Project Goal
>
> I'm consolidating utility code from many repositories into a single, modular, extensible Python framework. This system will serve as a flexible Swiss Army knife for use across various scientific and development projects.
>
> ### Architecture I Want to Build
>
> 1. **Base Class**
>
>    - Create a `BaseUtils` module with core shared logic used by all other utility modules.
>
> 2. **Shared Environment**
>
>    - Create a `SharedEnvironment` class responsible for:
>      - Loading configuration files (like `config.yaml`)
>      - Managing secrets or authentication tokens
>      - Providing a central interface for runtime config
>
> 3. **Modular Utilities**
>
>    - Create the following utility modules (each as a class in its own file):
>      - `NotebookUtils`
>      - `KBaseAPI`
>      - `KBGenomeUtils`
>      - `MSUtils`
>      - `KBModelUtil`
>    - Each module should:
>      - Inherit from `BaseUtils`
>      - Optionally make use of `SharedEnvironment`
>      - Provide focused, encapsulated functionality
>
> 4. **Composable Design**
>    - Support user-defined composite classes via multiple inheritance:
>      ```python
>      class MyUtils(KBaseAPI, KBGenomeUtils, SharedEnvironment):
>          pass
>      ```
>    - These classes should allow flexible selection of only the modules a user needs.
>
> ### What to Do Now
>
> - Scaffold the necessary Python files and class stubs for the architecture above.
> - Write light docstrings for each class so I can navigate them easily.
> - Then, help me migrate existing code into the appropriate utility modules while adhering to the architecture (I’ll paste it in as we go).
> - Keep everything aligned with the conventions from my Cookiecutter template.
