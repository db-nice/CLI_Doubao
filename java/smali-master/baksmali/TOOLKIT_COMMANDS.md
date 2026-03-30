# Baksmali Toolkit Commands

## `subclasses`

Used to locate child classes from a class descriptor or list classes under a package path.
When the input is a real class, output includes related inheritance entries from `.super` and `.implements`.
Dot form like `LNene.epbox.IlIIllIllI.IIIlIIIIll$100000000;` is also accepted and normalized automatically.

| Command | Alias | Purpose |
| --- | --- | --- |
| `subclasses` | `subclass`, `children`, `sub` | Search subclasses or package child classes |

| Option | Short | Meaning |
| --- | --- | --- |
| `--smali-dir` | `-d` | Root smali directory |
| `--class` | `-c` | Class descriptor or package descriptor |
| `--output` | `-o` | Output file path |
| `--recursive` / `--transitive` | `-r` | Recursive search |
| `--mode auto` | `-m auto` | Auto detect class mode or package mode |
| `--mode inheritance` | `-m inheritance` | Force `extends` / `implements` search |
| `--mode package` | `-m package` | Force package child scan |

### Full command examples

Find direct subclasses of a class:

```bat
java -jar baksmali-xxx-fat.jar subclasses --smali-dir F:\tmp\smali_out --class "Lpkg/Base;" --mode inheritance -o children.txt
```

Find all subclasses recursively by `extends` / `implements`:

```bat
java -jar baksmali-xxx-fat.jar subclasses --smali-dir F:\tmp\smali_out --class "Lpkg/Base;" --mode inheritance --recursive true -o children_all.txt
```

Find inheritance relations from a concrete class with dot notation:

```bat
java -jar baksmali-xxx-fat.jar subclasses --smali-dir F:\tmp\smali_out --class "LNene.epbox.IlIIllIllI.IIIlIIIIll$100000000;" -o inherit_links.txt
```

Find all classes under a package:

```bat
java -jar baksmali-xxx-fat.jar subclasses --smali-dir F:\tmp\smali_out --class "LNene/epbox;" --mode package -o package_children.txt
```

Find all classes under a package and continue following `extends` / `implements` chains:

```bat
java -jar baksmali-xxx-fat.jar subclasses --smali-dir F:\tmp\smali_out --class "LNene/epbox;" --recursive true -o package_chain.txt
```

### Short command examples

Direct class search:

```bat
java -jar baksmali-xxx-fat.jar sub -d F:\tmp\smali_out -c "Lpkg/Base;" -m inheritance -o children.txt
```

Recursive class search:

```bat
java -jar baksmali-xxx-fat.jar sub -d F:\tmp\smali_out -c "Lpkg/Base;" -m inheritance -r true -o children_all.txt
```

Package search:

```bat
java -jar baksmali-xxx-fat.jar sub -d F:\tmp\smali_out -c "LNene/epbox;" -o package_children.txt
```

Package search with inheritance expansion:

```bat
java -jar baksmali-xxx-fat.jar sub -d F:\tmp\smali_out -c "LNene/epbox;" -r true -o package_chain.txt
```

## `xref`

Used to locate method, field and class references in dex.

| Command | Alias | Purpose |
| --- | --- | --- |
| `xref` | `refs`, `ref` | Search references and call chains |

### Extra options

| Option | Meaning |
| --- | --- |
| `--chain` / `--chain-input` | Input chain text and infer target descriptor from chain |
| `--chain-file` | Read chain text from file |
| `--risk-only true` | Only keep crypto/obfuscation related references |
| `--result-cache` / `--cache-file` | Cache rendered xref output to file |
| `--reuse-cache true` | Reuse cached output and skip re-scan when query is same |
| `--write-cache true` | Save current output into cache |
| `--cache-max-entries` | Limit cache records, default `200` |

`xref` output now includes dependency edges (`from -> to`) and risk tags (`riskCrypto`, `riskObfuscation`) in both `text` and `json`.

### Full command examples

```bat
java -jar baksmali-xxx-fat.jar xref --method "Lcom/pkg/Cls;->foo(I)V" classes.dex --depth 2 --format json -o xref.json
java -jar baksmali-xxx-fat.jar xref --field "Lcom/pkg/Cls;->value:I" classes.dex --format text
java -jar baksmali-xxx-fat.jar xref --class "Lcom/pkg/Cls;" classes.dex --exclude-android-sdk true -o class_refs.json
java -jar baksmali-xxx-fat.jar xref --class "Lcom/old/Cls;" --new "Lcom/new/Cls;" --format map classes.dex -o map.csv
java -jar baksmali-xxx-fat.jar xref --chain "Lcom/a/A;->x()V => Lcom/b/B;->y()V" classes.dex --format json -o chain_xref.json
java -jar baksmali-xxx-fat.jar xref --method "Lcom/app/Decryptor;->decode(Ljava/lang/String;)Ljava/lang/String;" --risk-only true classes.dex --format text -o risk_refs.txt
```

### Notes

- `subclasses` in `auto` mode keeps the old inheritance logic when the descriptor is an actual class.
- If the input descriptor is not an actual class but matches a package path such as `LNene/epbox;`, it switches to package scan automatically.
- In package mode, `--recursive true` also follows `extends` and `implements` descendants for easier chain retrieval.
