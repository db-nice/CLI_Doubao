/*
 * Copyright 2026
 *
 * Cross-reference search support for baksmali.
 */
package org.jf.baksmali;

import com.beust.jcommander.JCommander;
import com.beust.jcommander.Parameter;
import com.beust.jcommander.Parameters;
import com.beust.jcommander.validators.PositiveInteger;
import org.jf.baksmali.analysis.MethodDescriptorParser;
import org.jf.dexlib2.formatter.DexFormatter;
import org.jf.dexlib2.iface.ClassDef;
import org.jf.dexlib2.iface.Field;
import org.jf.dexlib2.iface.Method;
import org.jf.dexlib2.iface.MethodImplementation;
import org.jf.dexlib2.iface.debug.DebugItem;
import org.jf.dexlib2.iface.debug.LineNumber;
import org.jf.dexlib2.iface.instruction.DualReferenceInstruction;
import org.jf.dexlib2.iface.instruction.Instruction;
import org.jf.dexlib2.iface.instruction.ReferenceInstruction;
import org.jf.dexlib2.iface.reference.CallSiteReference;
import org.jf.dexlib2.iface.reference.FieldReference;
import org.jf.dexlib2.iface.reference.MethodHandleReference;
import org.jf.dexlib2.iface.reference.MethodProtoReference;
import org.jf.dexlib2.iface.reference.MethodReference;
import org.jf.dexlib2.iface.reference.Reference;
import org.jf.dexlib2.iface.reference.StringReference;
import org.jf.dexlib2.iface.reference.TypeReference;
import org.jf.dexlib2.immutable.reference.ImmutableFieldReference;
import org.jf.dexlib2.immutable.reference.ImmutableMethodReference;
import org.jf.util.StringUtils;
import org.jf.util.jcommander.ExtendedParameter;
import org.jf.util.jcommander.ExtendedParameters;

import javax.annotation.Nonnull;
import javax.annotation.Nullable;
import java.io.BufferedReader;
import java.io.BufferedWriter;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.StringWriter;
import java.io.OutputStreamWriter;
import java.io.Writer;
import java.nio.charset.StandardCharsets;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Base64;
import java.util.Collections;
import java.util.Deque;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

@Parameters(commandDescription = "Find references to a method, field or class inside a dex file.")
@ExtendedParameters(
        commandName = "xref",
        commandAliases = { "refs", "ref" })
public class XrefCommand extends DexInputCommand {
    private static final Pattern CHAIN_METHOD_PATTERN =
            Pattern.compile("L[^;\\s]+;->[^\\s(]+\\([^\\)]*\\)[^\\s,\\]\\)]+");
    private static final Pattern CHAIN_FIELD_PATTERN =
            Pattern.compile("L[^;\\s]+;->[^:\\s]+:[^\\s,\\]\\)]+");
    private static final Pattern CHAIN_CLASS_PATTERN =
            Pattern.compile("L[^;\\s]+;");

    private static final String[] SDK_PREFIXES = new String[] {
            "Landroid/",
            "Ljava/",
            "Ljavax/",
            "Ldalvik/",
            "Lkotlin/",
            "Lkotlinx/",
            "Lsun/",
            "Lcom/android/"
    };
    private static final String[] CRYPTO_DESCRIPTOR_HINTS = new String[] {
            "Ljavax/crypto/",
            "Ljava/security/",
            "Landroid/util/Base64;",
            "Lorg/bouncycastle/",
            "Ljava/util/zip/",
            "Ljavax/net/ssl/"
    };
    private static final String[] CRYPTO_NAME_HINTS = new String[] {
            "encrypt",
            "decrypt",
            "cipher",
            "digest",
            "sha",
            "md5",
            "aes",
            "rsa",
            "des",
            "base64",
            "decode",
            "encode",
            "xor"
    };

    private static final String DEFAULT_REGISTRY_FILENAME = "component-registry.txt";
    private static final String DEFAULT_MOSUY_FILENAME = "mosuy.log";
    private static final String DEFAULT_RESULT_CACHE_FILENAME = "xref-result-cache.db";
    private static final String SAVE_DIR_PROPERTY = "xref.saveDir";
    private static final String SAVE_DIR_ENV = "XREF_SAVE_DIR";

    @Parameter(names = {"-h", "-?", "--help"}, help = true,
            description = "Show usage information")
    private boolean help;

    @Parameter(names = "--method",
            description = "Target method descriptor, for example Lpkg/Cls;->name(I)V")
    @ExtendedParameter(argumentNames = "descriptor")
    private String methodDescriptor;

    @Parameter(names = "--field",
            description = "Target field descriptor, for example Lpkg/Cls;->value:I")
    @ExtendedParameter(argumentNames = "descriptor")
    private String fieldDescriptor;

    @Parameter(names = "--class",
            description = "Target class descriptor, for example Lpkg/Cls;")
    @ExtendedParameter(argumentNames = "descriptor")
    private String classDescriptor;

    @Parameter(names = {"--max-depth", "--depth"},
            description = "Reverse call-chain depth when using --method. Defaults to 1.",
            validateWith = PositiveInteger.class)
    @ExtendedParameter(argumentNames = "n")
    private int maxDepth = 1;

    @Parameter(names = {"--include-call-chains", "--chains"}, arity = 1,
            description = "Include reverse call-chain expansion for method targets. True by default.")
    @ExtendedParameter(argumentNames = "boolean")
    private boolean includeCallChains = true;

    @Parameter(names = {"--include-inheritance", "--inheritance"}, arity = 1,
            description = "For class targets, include superclass, interface and signature matches. True by default.")
    @ExtendedParameter(argumentNames = "boolean")
    private boolean includeInheritance = true;

    @Parameter(names = {"--exclude-android-sdk", "--exclude-sdk"}, arity = 1,
            description = "Skip caller classes under common Android and Java framework package prefixes.")
    @ExtendedParameter(argumentNames = "boolean")
    private boolean excludeAndroidSdk = false;

    @Parameter(names = {"--save-dir", "--save-root"},
            description = "Base directory for logs/config files (can also be set via -Dxref.saveDir or XREF_SAVE_DIR).")
    @ExtendedParameter(argumentNames = "dir")
    private String saveDir;

    @Parameter(names = {"--component-registry", "--registry"},
            description = "Path to component registry file for omission rules.")
    @ExtendedParameter(argumentNames = "file")
    private String componentRegistryPath;

    @Parameter(names = {"--omit-registered-components", "--omit-registry"}, arity = 1,
            description = "Omit detailed tracking for components listed in registry. True by default.")
    @ExtendedParameter(argumentNames = "boolean")
    private boolean omitRegisteredComponents = true;

    @Parameter(names = {"--mosuy-log", "--mosuy"},
            description = "Write errors to this log file (resolved relative to save dir).")
    @ExtendedParameter(argumentNames = "file")
    private String mosuyLogPath;

    @Parameter(names = {"--result-cache", "--cache-file"},
            description = "Cache file path for xref rendered output (resolved relative to save dir).")
    @ExtendedParameter(argumentNames = "file")
    private String resultCachePath;

    @Parameter(names = {"--reuse-cache"}, arity = 1,
            description = "Reuse cached xref output when the same dex/target/options are requested. True by default.")
    @ExtendedParameter(argumentNames = "boolean")
    private boolean reuseResultCache = true;

    @Parameter(names = {"--write-cache"}, arity = 1,
            description = "Write current xref output to cache for later reuse. True by default.")
    @ExtendedParameter(argumentNames = "boolean")
    private boolean writeResultCache = true;

    @Parameter(names = {"--cache-max-entries"},
            description = "Maximum cached xref results to keep. Defaults to 200.",
            validateWith = PositiveInteger.class)
    @ExtendedParameter(argumentNames = "n")
    private int cacheMaxEntries = 200;

    @Parameter(names = {"-o", "--output"},
            description = "Write the results to a file instead of stdout.")
    @ExtendedParameter(argumentNames = "file")
    private String outputPath;

    @Parameter(names = "--format",
            description = "Output format: json, text or map. Defaults to json.")
    @ExtendedParameter(argumentNames = "format")
    private String format = "json";

    @Parameter(names = {"--chain", "--chain-input"},
            description = "Reference chain text input. If --method/--field/--class is omitted, the target is inferred from this chain.")
    @ExtendedParameter(argumentNames = "text")
    private String chainInput;

    @Parameter(names = {"--chain-file"},
            description = "Read reference chain text from file. Works like --chain.")
    @ExtendedParameter(argumentNames = "file")
    private String chainFilePath;

    @Parameter(names = {"--risk-only"}, arity = 1,
            description = "Only output references/call sites with crypto or obfuscation risk tags.")
    @ExtendedParameter(argumentNames = "boolean")
    private boolean riskOnly = false;

    @Parameter(names = {"--new", "--new-descriptor", "--rename-to"},
            description = "New descriptor used for map output.")
    @ExtendedParameter(argumentNames = "descriptor")
    private String newDescriptor;

    @Nonnull
    private final List<String> parsedChainDescriptors = new ArrayList<String>();

    public XrefCommand(@Nonnull List<JCommander> commandAncestors) {
        super(commandAncestors);
    }

    @Override
    public void run() {
        if (help || inputList == null || inputList.isEmpty()) {
            usage();
            return;
        }

        if (inputList.size() > 1) {
            System.err.println("Too many files specified");
            usage();
            return;
        }

        SearchTarget target;
        try {
            target = parseTarget();
        } catch (IllegalArgumentException ex) {
            System.err.println(ex.getMessage());
            usage();
            return;
        }

        String formatValue = format == null ? "json" : format.trim();
        boolean isMapFormat = "map".equalsIgnoreCase(formatValue) || "mapping".equalsIgnoreCase(formatValue);
        if (!"json".equalsIgnoreCase(formatValue) && !"text".equalsIgnoreCase(formatValue) && !isMapFormat) {
            System.err.println("Invalid format: " + format + ". Expected json, text or map.");
            System.exit(-1);
        }

        String mappingNewDescriptor = null;
        if (isMapFormat) {
            try {
                mappingNewDescriptor = resolveMappingNewDescriptor(target);
            } catch (IllegalArgumentException ex) {
                System.err.println(ex.getMessage());
                usage();
                return;
            }
        }

        SavePaths savePaths = resolveSavePaths();
        LogSink errorLog = LogSink.open(savePaths.mosuyLogFile);

        ResultCacheStore resultCacheStore = null;
        if ((reuseResultCache || writeResultCache) && savePaths.resultCacheFile != null) {
            resultCacheStore = ResultCacheStore.open(savePaths.resultCacheFile);
        }

        ComponentRegistry registry = ComponentRegistry.empty();
        if (savePaths.registryFile != null) {
            ComponentRegistry.LoadResult loadResult = ComponentRegistry.load(savePaths.registryFile);
            registry = loadResult.getRegistry();
            if (errorLog != null) {
                for (String error : loadResult.getErrors()) {
                    errorLog.write("registry", error);
                }
            }
        }

        String input = inputList.get(0);
        String cacheKey = buildCacheKey(input, target, formatValue, mappingNewDescriptor, savePaths.registryFile);
        if (reuseResultCache && resultCacheStore != null) {
            String cachedOutput = resultCacheStore.get(cacheKey);
            if (cachedOutput != null) {
                try {
                    writeRenderedOutput(cachedOutput);
                    System.err.println("xref cache hit");
                    return;
                } catch (IOException ex) {
                    if (errorLog != null) {
                        errorLog.write("error", "Failed writing cached xref output: " + ex.getMessage());
                    }
                }
            }
        }

        loadDexFile(input);

        SearchResults results = collectResults(target, registry, errorLog);
        try {
            String renderedOutput = renderResults(results, formatValue, mappingNewDescriptor);
            writeRenderedOutput(renderedOutput);
            if (writeResultCache && resultCacheStore != null) {
                resultCacheStore.put(cacheKey, renderedOutput, cacheMaxEntries);
                resultCacheStore.save();
            }
        } catch (IOException ex) {
            if (errorLog != null) {
                errorLog.write("error", "Failed to write xref results: " + ex.getMessage());
            }
            System.err.println("Failed to write xref results");
            ex.printStackTrace(System.err);
            System.exit(-1);
        } finally {
            if (errorLog != null) {
                errorLog.close();
            }
        }
    }

    @Nonnull
    private SearchTarget parseTarget() {
        parsedChainDescriptors.clear();

        String targetFromChain = resolveTargetDescriptorFromChainInput();
        int targetCount = 0;
        if (!isBlank(methodDescriptor)) {
            targetCount++;
        }
        if (!isBlank(fieldDescriptor)) {
            targetCount++;
        }
        if (!isBlank(classDescriptor)) {
            targetCount++;
        }

        if (targetCount > 1) {
            throw new IllegalArgumentException("Specify exactly one of --method, --field or --class");
        }

        if (targetCount == 0) {
            if (isBlank(targetFromChain)) {
                throw new IllegalArgumentException("Specify one target via --method/--field/--class, or provide --chain/--chain-file");
            }
            return buildTargetFromDescriptor(targetFromChain);
        }

        if (!isBlank(methodDescriptor)) {
            ImmutableMethodReference methodReference = MethodDescriptorParser.parse(methodDescriptor);
            return new SearchTarget(
                    TargetKind.METHOD,
                    DexFormatter.INSTANCE.getMethodDescriptor(methodReference),
                    methodReference.getDefiningClass());
        }

        if (!isBlank(fieldDescriptor)) {
            ImmutableFieldReference fieldReference = parseFieldDescriptor(fieldDescriptor);
            return new SearchTarget(
                    TargetKind.FIELD,
                    DexFormatter.INSTANCE.getFieldDescriptor(fieldReference),
                    fieldReference.getDefiningClass());
        }

        String normalizedClass = normalizeClassDescriptor(classDescriptor);
        return new SearchTarget(TargetKind.CLASS, normalizedClass, normalizedClass);
    }

    @Nonnull
    private SearchTarget buildTargetFromDescriptor(@Nonnull String descriptor) {
        String trimmed = descriptor.trim();
        if (trimmed.contains("->")) {
            if (trimmed.contains("(")) {
                ImmutableMethodReference methodReference = MethodDescriptorParser.parse(trimmed);
                return new SearchTarget(
                        TargetKind.METHOD,
                        DexFormatter.INSTANCE.getMethodDescriptor(methodReference),
                        methodReference.getDefiningClass());
            }
            ImmutableFieldReference fieldReference = parseFieldDescriptor(trimmed);
            return new SearchTarget(
                    TargetKind.FIELD,
                    DexFormatter.INSTANCE.getFieldDescriptor(fieldReference),
                    fieldReference.getDefiningClass());
        }
        String normalizedClass = normalizeClassDescriptor(trimmed);
        return new SearchTarget(TargetKind.CLASS, normalizedClass, normalizedClass);
    }

    @Nonnull
    private SavePaths resolveSavePaths() {
        File baseDir = null;
        if (!isBlank(saveDir)) {
            baseDir = new File(saveDir.trim());
        } else {
            String property = System.getProperty(SAVE_DIR_PROPERTY);
            if (!isBlank(property)) {
                baseDir = new File(property.trim());
            } else {
                String env = System.getenv(SAVE_DIR_ENV);
                if (!isBlank(env)) {
                    baseDir = new File(env.trim());
                }
            }
        }

        if (baseDir == null) {
            String userDir = System.getProperty("user.dir");
            if (isBlank(userDir)) {
                baseDir = new File(".");
            } else {
                baseDir = new File(userDir);
            }
        }

        baseDir = baseDir.getAbsoluteFile();
        if (!baseDir.exists() && !baseDir.mkdirs()) {
            System.err.println("Unable to create save directory: " + baseDir);
        }

        File registryFile = null;
        if (!isBlank(componentRegistryPath)) {
            registryFile = resolvePathAgainst(baseDir, componentRegistryPath);
        } else {
            File candidate = new File(baseDir, DEFAULT_REGISTRY_FILENAME);
            if (candidate.isFile()) {
                registryFile = candidate;
            }
        }

        File mosuyFile;
        if (!isBlank(mosuyLogPath)) {
            mosuyFile = resolvePathAgainst(baseDir, mosuyLogPath);
        } else {
            mosuyFile = new File(baseDir, DEFAULT_MOSUY_FILENAME);
        }

        File resultCacheFile;
        if (!isBlank(resultCachePath)) {
            resultCacheFile = resolvePathAgainst(baseDir, resultCachePath);
        } else {
            resultCacheFile = new File(baseDir, DEFAULT_RESULT_CACHE_FILENAME);
        }

        return new SavePaths(baseDir, registryFile, mosuyFile, resultCacheFile);
    }

    @Nonnull
    private File resolvePathAgainst(@Nonnull File baseDir, @Nonnull String path) {
        File file = new File(path);
        if (!file.isAbsolute()) {
            file = new File(baseDir, path);
        }
        return file.getAbsoluteFile();
    }
    @Nonnull
    private String resolveMappingNewDescriptor(@Nonnull SearchTarget target) {
        if (isBlank(newDescriptor)) {
            throw new IllegalArgumentException("Map format requires --new <descriptor>");
        }

        if (target.kind == TargetKind.METHOD) {
            ImmutableMethodReference methodReference = MethodDescriptorParser.parse(newDescriptor);
            return DexFormatter.INSTANCE.getMethodDescriptor(methodReference);
        }
        if (target.kind == TargetKind.FIELD) {
            ImmutableFieldReference fieldReference = parseFieldDescriptor(newDescriptor);
            return DexFormatter.INSTANCE.getFieldDescriptor(fieldReference);
        }
        if (target.kind == TargetKind.CLASS) {
            return normalizeClassDescriptor(newDescriptor);
        }
        throw new IllegalArgumentException("Unsupported target type for map output");
    }

    @Nullable
    private String resolveTargetDescriptorFromChainInput() {
        String chainText = readChainTextInput();
        if (isBlank(chainText)) {
            return null;
        }

        LinkedHashSet<String> descriptors = new LinkedHashSet<String>();
        List<String> methodCandidates = findPatternMatches(chainText, CHAIN_METHOD_PATTERN);
        if (!methodCandidates.isEmpty()) {
            for (String candidate : methodCandidates) {
                try {
                    ImmutableMethodReference methodReference = MethodDescriptorParser.parse(candidate);
                    descriptors.add(DexFormatter.INSTANCE.getMethodDescriptor(methodReference));
                } catch (IllegalArgumentException ex) {
                    // ignore malformed chain fragments
                }
            }
        }

        if (descriptors.isEmpty()) {
            List<String> fieldCandidates = findPatternMatches(chainText, CHAIN_FIELD_PATTERN);
            for (String candidate : fieldCandidates) {
                try {
                    ImmutableFieldReference fieldReference = parseFieldDescriptor(candidate);
                    descriptors.add(DexFormatter.INSTANCE.getFieldDescriptor(fieldReference));
                } catch (IllegalArgumentException ex) {
                    // ignore malformed chain fragments
                }
            }
        }

        if (descriptors.isEmpty()) {
            List<String> classCandidates = findPatternMatches(chainText, CHAIN_CLASS_PATTERN);
            for (String candidate : classCandidates) {
                try {
                    descriptors.add(normalizeClassDescriptor(candidate));
                } catch (IllegalArgumentException ex) {
                    // ignore malformed chain fragments
                }
            }
        }

        if (descriptors.isEmpty()) {
            return null;
        }

        parsedChainDescriptors.addAll(descriptors);
        return parsedChainDescriptors.get(parsedChainDescriptors.size() - 1);
    }

    @Nullable
    private String readChainTextInput() {
        StringBuilder builder = new StringBuilder();
        if (!isBlank(chainInput)) {
            builder.append(chainInput.trim());
        }

        if (!isBlank(chainFilePath)) {
            File file = new File(chainFilePath.trim());
            if (!file.isFile()) {
                throw new IllegalArgumentException("Invalid --chain-file path: " + chainFilePath);
            }
            BufferedReader reader = null;
            try {
                reader = new BufferedReader(new InputStreamReader(new FileInputStream(file), StandardCharsets.UTF_8));
                String line;
                while ((line = reader.readLine()) != null) {
                    if (builder.length() > 0) {
                        builder.append('\n');
                    }
                    builder.append(line);
                }
            } catch (IOException ex) {
                throw new IllegalArgumentException("Failed reading --chain-file: " + ex.getMessage());
            } finally {
                if (reader != null) {
                    try {
                        reader.close();
                    } catch (IOException ex) {
                        // ignore
                    }
                }
            }
        }

        if (builder.length() == 0) {
            return null;
        }
        return builder.toString();
    }

    @Nonnull
    private List<String> findPatternMatches(@Nonnull String text, @Nonnull Pattern pattern) {
        List<String> values = new ArrayList<String>();
        Matcher matcher = pattern.matcher(text);
        while (matcher.find()) {
            values.add(matcher.group());
        }
        return values;
    }

    @Nonnull
    private SearchResults collectResults(@Nonnull SearchTarget target,
                                             @Nonnull ComponentRegistry registry,
                                             @Nullable LogSink errorLog) {
        SearchResults results = new SearchResults(target);
        results.inputChainDescriptors.addAll(parsedChainDescriptors);
        DexFormatter formatter = DexFormatter.INSTANCE;
        Map<String, List<CallSite>> callersByCallee = new LinkedHashMap<String, List<CallSite>>();
        Set<String> definitionKeys = new LinkedHashSet<String>();

        for (ClassDef classDef : dexFile.getClasses()) {
            if (excludeAndroidSdk && shouldSkipFrameworkClass(classDef.getType(), target.ownerDescriptor)) {
                continue;
            }

            if (target.kind == TargetKind.CLASS && classDef.getType().equals(target.descriptor)) {
                addDefinition(results.definitions, definitionKeys,
                        new DefinitionRecord("class-definition", classDef.getType(), classDef.getType(),
                                classDef.getSourceFile(), -1));
            }

            if (target.kind == TargetKind.CLASS && includeInheritance) {
                if (target.descriptor.equals(classDef.getSuperclass())) {
                    results.references.add(new ReferenceMatch(
                            "extends",
                            classDef.getType(),
                            null,
                            classDef.getSourceFile(),
                            -1,
                            -1,
                            -1,
                            null,
                            classDef.getSuperclass(),
                            false,
                            false,
                            false));
                }
                for (String iface : classDef.getInterfaces()) {
                    if (target.descriptor.equals(iface)) {
                        results.references.add(new ReferenceMatch(
                                "implements",
                                classDef.getType(),
                                null,
                                classDef.getSourceFile(),
                                -1,
                                -1,
                                -1,
                                null,
                                iface,
                                false,
                                false,
                                false));
                    }
                }
            }

            for (Field field : classDef.getFields()) {
                String currentFieldDescriptor = formatter.getFieldDescriptor(field);
                if (target.kind == TargetKind.FIELD && target.descriptor.equals(currentFieldDescriptor)) {
                    addDefinition(results.definitions, definitionKeys,
                            new DefinitionRecord("field-definition", classDef.getType(), currentFieldDescriptor,
                                    classDef.getSourceFile(), -1));
                }

                if (target.kind == TargetKind.CLASS && includeInheritance && target.descriptor.equals(field.getType())) {
                    results.references.add(new ReferenceMatch(
                            "field-declaration",
                            classDef.getType(),
                            null,
                            classDef.getSourceFile(),
                            -1,
                            -1,
                            -1,
                            null,
                            currentFieldDescriptor,
                            false,
                            false,
                            false));
                }
            }

            for (Method method : classDef.getMethods()) {
                String callerDescriptor = formatter.getMethodDescriptor(method);
                MethodImplementation methodImplementation = method.getImplementation();
                DebugLineResolver lineResolver = new DebugLineResolver(methodImplementation);
                int methodStartLine = lineResolver.getMethodStartLine();

                if (target.kind == TargetKind.METHOD && target.descriptor.equals(callerDescriptor)) {
                    addDefinition(results.definitions, definitionKeys,
                            new DefinitionRecord("method-definition", classDef.getType(), callerDescriptor,
                                    classDef.getSourceFile(), methodStartLine));
                }

                if (target.kind == TargetKind.CLASS && includeInheritance) {
                    if (containsType(method.getParameterTypes(), target.descriptor)) {
                        results.references.add(new ReferenceMatch(
                                "method-parameter",
                                classDef.getType(),
                                callerDescriptor,
                                classDef.getSourceFile(),
                                methodStartLine,
                                -1,
                                -1,
                                null,
                                callerDescriptor,
                                false,
                                false,
                                false));
                    }
                    if (target.descriptor.equals(method.getReturnType())) {
                        results.references.add(new ReferenceMatch(
                                "method-return",
                                classDef.getType(),
                                callerDescriptor,
                                classDef.getSourceFile(),
                                methodStartLine,
                                -1,
                                -1,
                                null,
                                callerDescriptor,
                                false,
                                false,
                                false));
                    }
                }

                if (methodImplementation == null) {
                    continue;
                }

                int codeAddress = 0;
                for (Instruction instruction : methodImplementation.getInstructions()) {
                    int lineNumber = lineResolver.getLineNumber(codeAddress);

                    if (instruction instanceof ReferenceInstruction) {
                        Reference reference = ((ReferenceInstruction) instruction).getReference();
                        addInstructionMatchIfNeeded(results.references, target, classDef, callerDescriptor,
                                classDef.getSourceFile(), methodStartLine, lineNumber, codeAddress,
                                instruction.getOpcode().name, reference, false);
                        addCallSiteIfNeeded(callersByCallee, classDef, callerDescriptor, classDef.getSourceFile(),
                                methodStartLine, lineNumber, codeAddress, instruction.getOpcode().name, reference, registry);
                    }

                    if (instruction instanceof DualReferenceInstruction) {
                        Reference reference2 = ((DualReferenceInstruction) instruction).getReference2();
                        addInstructionMatchIfNeeded(results.references, target, classDef, callerDescriptor,
                                classDef.getSourceFile(), methodStartLine, lineNumber, codeAddress,
                                instruction.getOpcode().name, reference2, true);
                        addCallSiteIfNeeded(callersByCallee, classDef, callerDescriptor, classDef.getSourceFile(),
                                methodStartLine, lineNumber, codeAddress, instruction.getOpcode().name, reference2, registry);
                    }

                    codeAddress += instruction.getCodeUnits();
                }
            }
        }

        if (target.kind == TargetKind.METHOD && includeCallChains) {
            results.callChains.addAll(buildCallChains(target.descriptor, callersByCallee));
        }

        fillDependencyEdges(results);

        return results;
    }

    private void addInstructionMatchIfNeeded(@Nonnull List<ReferenceMatch> matches,
                                             @Nonnull SearchTarget target,
                                             @Nonnull ClassDef classDef,
                                             @Nonnull String callerDescriptor,
                                             @Nullable String sourceFile,
                                             int methodStartLine,
                                             int lineNumber,
                                             int codeAddress,
                                             @Nonnull String opcode,
                                             @Nonnull Reference reference,
                                             boolean dualReference) {
        if (!matchesTarget(target, reference)) {
            return;
        }

        String referenceDescriptor = getReferenceDescriptor(reference);
        RiskTag riskTag = analyzeRisk(callerDescriptor, referenceDescriptor, opcode);
        if (riskOnly && !riskTag.hasRisk()) {
            return;
        }

        matches.add(new ReferenceMatch(
                getReferenceKind(target, reference),
                classDef.getType(),
                callerDescriptor,
                sourceFile,
                methodStartLine,
                lineNumber,
                codeAddress,
                opcode,
                referenceDescriptor,
                dualReference,
                riskTag.cryptoRelated,
                riskTag.obfuscationRelated));
    }

    private void addCallSiteIfNeeded(@Nonnull Map<String, List<CallSite>> callersByCallee,
                                     @Nonnull ClassDef classDef,
                                     @Nonnull String callerDescriptor,
                                     @Nullable String sourceFile,
                                     int methodStartLine,
                                     int lineNumber,
                                     int codeAddress,
                                     @Nonnull String opcode,
                                     @Nonnull Reference reference,
                                     @Nonnull ComponentRegistry registry) {
        if (!(reference instanceof MethodReference)) {
            return;
        }

        String calleeDescriptor = DexFormatter.INSTANCE.getMethodDescriptor((MethodReference) reference);
        List<CallSite> callSites = callersByCallee.get(calleeDescriptor);
        if (callSites == null) {
            callSites = new ArrayList<CallSite>();
            callersByCallee.put(calleeDescriptor, callSites);
        }

        boolean omitted = omitRegisteredComponents && registry.matchesMethod(callerDescriptor, classDef.getType());
        String omittedReason = omitted ? "component_registry" : null;
        RiskTag riskTag = analyzeRisk(callerDescriptor, calleeDescriptor, opcode);
        if (riskOnly && !riskTag.hasRisk()) {
            return;
        }

        callSites.add(new CallSite(
                classDef.getType(),
                callerDescriptor,
                sourceFile,
                methodStartLine,
                lineNumber,
                codeAddress,
                opcode,
                calleeDescriptor,
                omitted,
                omittedReason,
                riskTag.cryptoRelated,
                riskTag.obfuscationRelated));
    }

    private void fillDependencyEdges(@Nonnull SearchResults results) {
        Set<String> seen = new LinkedHashSet<String>();
        for (ReferenceMatch match : results.references) {
            String from = match.callerMethod != null ? match.callerMethod : match.callerClass;
            DependencyEdge edge = new DependencyEdge(
                    from,
                    match.referenceDescriptor,
                    match.kind,
                    match.sourceFile,
                    match.lineNumber,
                    match.opcode,
                    match.cryptoRelated,
                    match.obfuscationRelated);
            String key = edge.fromDescriptor + "|" + edge.toDescriptor + "|" + edge.kind + "|" + edge.lineNumber;
            if (seen.add(key)) {
                results.dependencyEdges.add(edge);
            }
        }
        for (CallChain callChain : results.callChains) {
            for (CallSite callSite : callChain.path) {
                DependencyEdge edge = new DependencyEdge(
                        callSite.callerDescriptor,
                        callSite.calleeDescriptor,
                        "call-chain",
                        callSite.sourceFile,
                        callSite.lineNumber,
                        callSite.opcode,
                        callSite.cryptoRelated,
                        callSite.obfuscationRelated);
                String key = edge.fromDescriptor + "|" + edge.toDescriptor + "|" + edge.kind + "|" + edge.lineNumber;
                if (seen.add(key)) {
                    results.dependencyEdges.add(edge);
                }
            }
        }
    }
    @Nonnull
    private List<CallChain> buildCallChains(@Nonnull String targetDescriptor,
                                            @Nonnull Map<String, List<CallSite>> callersByCallee) {
        List<CallChain> chains = new ArrayList<CallChain>();
        Set<String> seenChains = new LinkedHashSet<String>();
        Deque<CallSite> currentPath = new ArrayDeque<CallSite>();
        Set<String> activeMethods = new LinkedHashSet<String>();
        activeMethods.add(targetDescriptor);
        expandCallChains(targetDescriptor, callersByCallee, maxDepth, currentPath, activeMethods, chains, seenChains);
        return chains;
    }

    private void expandCallChains(@Nonnull String calleeDescriptor,
                                  @Nonnull Map<String, List<CallSite>> callersByCallee,
                                  int remainingDepth,
                                  @Nonnull Deque<CallSite> currentPath,
                                  @Nonnull Set<String> activeMethods,
                                  @Nonnull List<CallChain> chains,
                                  @Nonnull Set<String> seenChains) {
        if (remainingDepth <= 0) {
            return;
        }

        List<CallSite> callers = callersByCallee.get(calleeDescriptor);
        if (callers == null || callers.isEmpty()) {
            return;
        }

        for (CallSite callSite : callers) {
            if (activeMethods.contains(callSite.callerDescriptor)) {
                continue;
            }

            currentPath.addLast(callSite);
            CallChain chain = CallChain.fromNearestFirstPath(currentPath);
            if (seenChains.add(chain.toKey())) {
                chains.add(chain);
            }

            if (!callSite.omitted) {
                activeMethods.add(callSite.callerDescriptor);
                expandCallChains(callSite.callerDescriptor, callersByCallee, remainingDepth - 1,
                        currentPath, activeMethods, chains, seenChains);
                activeMethods.remove(callSite.callerDescriptor);
            }
            currentPath.removeLast();
        }
    }

    private boolean matchesTarget(@Nonnull SearchTarget target, @Nonnull Reference reference) {
        switch (target.kind) {
            case METHOD:
                return methodReferenceMatches(target.descriptor, reference);
            case FIELD:
                return fieldReferenceMatches(target.descriptor, reference);
            case CLASS:
                return classReferenceMatches(target.descriptor, reference);
            default:
                return false;
        }
    }

    private boolean methodReferenceMatches(@Nonnull String targetDescriptor, @Nonnull Reference reference) {
        if (reference instanceof MethodReference) {
            return targetDescriptor.equals(DexFormatter.INSTANCE.getMethodDescriptor((MethodReference) reference));
        }
        if (reference instanceof MethodHandleReference) {
            Reference memberReference = ((MethodHandleReference) reference).getMemberReference();
            return memberReference instanceof MethodReference
                    && targetDescriptor.equals(DexFormatter.INSTANCE.getMethodDescriptor((MethodReference) memberReference));
        }
        if (reference instanceof CallSiteReference) {
            MethodHandleReference methodHandle = ((CallSiteReference) reference).getMethodHandle();
            Reference memberReference = methodHandle.getMemberReference();
            return memberReference instanceof MethodReference
                    && targetDescriptor.equals(DexFormatter.INSTANCE.getMethodDescriptor((MethodReference) memberReference));
        }
        return false;
    }

    private boolean fieldReferenceMatches(@Nonnull String targetDescriptor, @Nonnull Reference reference) {
        if (reference instanceof FieldReference) {
            return targetDescriptor.equals(DexFormatter.INSTANCE.getFieldDescriptor((FieldReference) reference));
        }
        if (reference instanceof MethodHandleReference) {
            Reference memberReference = ((MethodHandleReference) reference).getMemberReference();
            return memberReference instanceof FieldReference
                    && targetDescriptor.equals(DexFormatter.INSTANCE.getFieldDescriptor((FieldReference) memberReference));
        }
        return false;
    }

    private boolean classReferenceMatches(@Nonnull String targetClass, @Nonnull Reference reference) {
        if (reference instanceof TypeReference) {
            return targetClass.equals(((TypeReference) reference).getType());
        }
        if (reference instanceof FieldReference) {
            FieldReference fieldReference = (FieldReference) reference;
            return targetClass.equals(fieldReference.getDefiningClass()) || targetClass.equals(fieldReference.getType());
        }
        if (reference instanceof MethodReference) {
            MethodReference methodReference = (MethodReference) reference;
            return targetClass.equals(methodReference.getDefiningClass())
                    || containsType(methodReference.getParameterTypes(), targetClass)
                    || targetClass.equals(methodReference.getReturnType());
        }
        if (reference instanceof MethodProtoReference) {
            MethodProtoReference protoReference = (MethodProtoReference) reference;
            return containsType(protoReference.getParameterTypes(), targetClass)
                    || targetClass.equals(protoReference.getReturnType());
        }
        if (reference instanceof MethodHandleReference) {
            return classReferenceMatches(targetClass, ((MethodHandleReference) reference).getMemberReference());
        }
        if (reference instanceof CallSiteReference) {
            CallSiteReference callSiteReference = (CallSiteReference) reference;
            return classReferenceMatches(targetClass, callSiteReference.getMethodHandle())
                    || classReferenceMatches(targetClass, callSiteReference.getMethodProto());
        }
        return false;
    }

    private boolean containsType(@Nullable Iterable<? extends CharSequence> types, @Nonnull String targetType) {
        if (types == null) {
            return false;
        }
        for (CharSequence type : types) {
            if (targetType.contentEquals(type)) {
                return true;
            }
        }
        return false;
    }

    @Nonnull
    private String getReferenceKind(@Nonnull SearchTarget target, @Nonnull Reference reference) {
        switch (target.kind) {
            case METHOD:
                if (reference instanceof MethodReference) {
                    return "invoke";
                }
                if (reference instanceof MethodHandleReference) {
                    return "method-handle";
                }
                return "call-site";
            case FIELD:
                if (reference instanceof FieldReference) {
                    return "field-access";
                }
                return "field-handle";
            case CLASS:
                if (reference instanceof TypeReference) {
                    return "type-reference";
                }
                if (reference instanceof FieldReference) {
                    return "field-reference";
                }
                if (reference instanceof MethodReference) {
                    return "method-reference";
                }
                if (reference instanceof MethodProtoReference) {
                    return "method-proto";
                }
                if (reference instanceof MethodHandleReference) {
                    return "method-handle";
                }
                if (reference instanceof StringReference) {
                    return "string-reference";
                }
                return "call-site";
            default:
                return "reference";
        }
    }

    @Nonnull
    private String getReferenceDescriptor(@Nonnull Reference reference) {
        DexFormatter formatter = DexFormatter.INSTANCE;
        if (reference instanceof MethodReference) {
            return formatter.getMethodDescriptor((MethodReference) reference);
        }
        if (reference instanceof FieldReference) {
            return formatter.getFieldDescriptor((FieldReference) reference);
        }
        if (reference instanceof TypeReference) {
            return ((TypeReference) reference).getType();
        }
        if (reference instanceof MethodProtoReference) {
            return formatter.getMethodProtoDescriptor((MethodProtoReference) reference);
        }
        if (reference instanceof MethodHandleReference) {
            return formatter.getMethodHandle((MethodHandleReference) reference);
        }
        if (reference instanceof CallSiteReference) {
            return formatter.getCallSite((CallSiteReference) reference);
        }
        if (reference instanceof StringReference) {
            return formatter.getQuotedString(((StringReference) reference).getString());
        }
        return reference.toString();
    }

    @Nonnull
    private String buildCacheKey(@Nonnull String inputPath,
                                 @Nonnull SearchTarget target,
                                 @Nonnull String formatValue,
                                 @Nullable String mappingNewDescriptor,
                                 @Nullable File registryFile) {
        File inputFile = new File(inputPath).getAbsoluteFile();
        long inputLength = inputFile.isFile() ? inputFile.length() : -1L;
        long inputMtime = inputFile.isFile() ? inputFile.lastModified() : -1L;
        long registryLength = registryFile != null && registryFile.isFile() ? registryFile.length() : -1L;
        long registryMtime = registryFile != null && registryFile.isFile() ? registryFile.lastModified() : -1L;

        StringBuilder builder = new StringBuilder();
        builder.append("v2|path=").append(inputFile.getPath())
                .append("|len=").append(inputLength)
                .append("|mtime=").append(inputMtime)
                .append("|targetKind=").append(target.kind.name())
                .append("|target=").append(target.descriptor)
                .append("|format=").append(formatValue)
                .append("|mapNew=").append(mappingNewDescriptor == null ? "" : mappingNewDescriptor)
                .append("|depth=").append(maxDepth)
                .append("|chains=").append(includeCallChains)
                .append("|inheritance=").append(includeInheritance)
                .append("|excludeSdk=").append(excludeAndroidSdk)
                .append("|omitRegistry=").append(omitRegisteredComponents)
                .append("|riskOnly=").append(riskOnly)
                .append("|registryPath=").append(registryFile == null ? "" : registryFile.getAbsolutePath())
                .append("|registryLen=").append(registryLength)
                .append("|registryMtime=").append(registryMtime)
                .append("|chainInput=").append(joinWithArrow(parsedChainDescriptors));
        return builder.toString();
    }

    @Nonnull
    private RiskTag analyzeRisk(@Nonnull String callerDescriptor,
                                @Nonnull String referenceDescriptor,
                                @Nonnull String opcode) {
        boolean crypto = containsCryptoHints(callerDescriptor)
                || containsCryptoHints(referenceDescriptor)
                || containsCryptoHints(opcode);
        boolean obfuscation = isObfuscatedDescriptor(callerDescriptor)
                || isObfuscatedDescriptor(referenceDescriptor);
        return new RiskTag(crypto, obfuscation);
    }

    private boolean containsCryptoHints(@Nullable String value) {
        if (value == null) {
            return false;
        }
        String lower = value.toLowerCase();
        for (String hint : CRYPTO_DESCRIPTOR_HINTS) {
            if (value.contains(hint)) {
                return true;
            }
        }
        for (String hint : CRYPTO_NAME_HINTS) {
            if (lower.contains(hint)) {
                return true;
            }
        }
        return false;
    }

    private boolean isObfuscatedDescriptor(@Nullable String descriptor) {
        if (descriptor == null || descriptor.isEmpty()) {
            return false;
        }
        if (!isAscii(descriptor)) {
            return true;
        }

        int start = descriptor.lastIndexOf('/');
        int end = descriptor.length();
        if (descriptor.contains("->")) {
            end = descriptor.indexOf("->");
        } else if (descriptor.endsWith(";")) {
            end = descriptor.length() - 1;
        }
        String classToken = descriptor.substring(Math.max(0, start + 1), Math.max(start + 1, end));
        if (looksObfuscatedToken(classToken)) {
            return true;
        }

        if (descriptor.contains("->")) {
            int nameStart = descriptor.indexOf("->") + 2;
            int nameEnd = descriptor.indexOf('(', nameStart);
            if (nameEnd < 0) {
                nameEnd = descriptor.indexOf(':', nameStart);
            }
            if (nameEnd < 0) {
                nameEnd = descriptor.length();
            }
            String member = descriptor.substring(nameStart, nameEnd);
            if (looksObfuscatedToken(member)) {
                return true;
            }
        }
        return false;
    }

    private boolean looksObfuscatedToken(@Nullable String token) {
        if (token == null || token.isEmpty()) {
            return false;
        }
        String normalized = token.replace("$", "");
        if (normalized.isEmpty()) {
            return false;
        }
        if (normalized.length() <= 2) {
            return true;
        }

        int ambiguous = 0;
        for (int i = 0; i < normalized.length(); i++) {
            char ch = normalized.charAt(i);
            if (ch == 'I' || ch == 'l' || ch == '1' || ch == 'O' || ch == '0') {
                ambiguous++;
            }
        }
        return ambiguous * 100 / normalized.length() >= 70;
    }

    private boolean isAscii(@Nonnull String value) {
        for (int i = 0; i < value.length(); i++) {
            if (value.charAt(i) > 0x7F) {
                return false;
            }
        }
        return true;
    }

    @Nonnull
    private String renderResults(@Nonnull SearchResults results,
                                 @Nonnull String formatValue,
                                 @Nullable String mappingNewDescriptor) throws IOException {
        StringWriter stringWriter = new StringWriter(4096);
        Writer writer = new BufferedWriter(stringWriter);
        if ("text".equalsIgnoreCase(formatValue)) {
            writeText(results, writer);
        } else if ("map".equalsIgnoreCase(formatValue) || "mapping".equalsIgnoreCase(formatValue)) {
            writeMapping(results, mappingNewDescriptor, writer);
        } else {
            writeJson(results, writer);
        }
        writer.flush();
        return stringWriter.toString();
    }

    private void writeRenderedOutput(@Nonnull String renderedOutput) throws IOException {
        Writer writer;
        boolean closeWriter = false;

        if (isBlank(outputPath)) {
            writer = new BufferedWriter(new OutputStreamWriter(System.out, StandardCharsets.UTF_8));
        } else {
            File outputFile = new File(outputPath);
            File parent = outputFile.getAbsoluteFile().getParentFile();
            if (parent != null && !parent.exists() && !parent.mkdirs()) {
                throw new IOException("Unable to create output directory: " + parent);
            }
            writer = new BufferedWriter(new OutputStreamWriter(new FileOutputStream(outputFile), StandardCharsets.UTF_8));
            closeWriter = true;
        }

        try {
            writer.write(renderedOutput);
            writer.flush();
        } finally {
            if (closeWriter) {
                writer.close();
            }
        }
    }

    private void writeText(@Nonnull SearchResults results, @Nonnull Writer writer) throws IOException {
        int riskRefCount = 0;
        for (ReferenceMatch match : results.references) {
            if (match.cryptoRelated || match.obfuscationRelated) {
                riskRefCount++;
            }
        }

        writer.write("TARGET\t");
        writer.write(results.target.kind.name().toLowerCase());
        writer.write('\t');
        writer.write(results.target.descriptor);
        writer.write('\n');

        if (!results.inputChainDescriptors.isEmpty()) {
            writer.write("CHAIN_INPUT\t");
            writer.write(joinWithArrow(results.inputChainDescriptors));
            writer.write('\n');
        }

        writer.write("SUMMARY\tdefinitions=");
        writer.write(Integer.toString(results.definitions.size()));
        writer.write("\treferences=");
        writer.write(Integer.toString(results.references.size()));
        writer.write("\triskReferences=");
        writer.write(Integer.toString(riskRefCount));
        writer.write("\tcallChains=");
        writer.write(Integer.toString(results.callChains.size()));
        writer.write("\tdependencies=");
        writer.write(Integer.toString(results.dependencyEdges.size()));
        writer.write('\n');

        for (DefinitionRecord definition : results.definitions) {
            writer.write("DEFINITION\tkind=");
            writer.write(definition.kind);
            writer.write("\towner=");
            writer.write(definition.ownerClass);
            writer.write("\tdescriptor=");
            writer.write(definition.descriptor);
            if (definition.sourceFile != null) {
                writer.write("\tsource=");
                writer.write(definition.sourceFile);
            }
            if (definition.methodStartLine >= 0) {
                writer.write("\tmethodStartLine=");
                writer.write(Integer.toString(definition.methodStartLine));
            }
            writer.write('\n');
        }

        for (ReferenceMatch match : results.references) {
            writer.write("REF\tkind=");
            writer.write(match.kind);
            writer.write("\tcallerClass=");
            writer.write(match.callerClass);
            if (match.callerMethod != null) {
                writer.write("\tcallerMethod=");
                writer.write(match.callerMethod);
            }
            if (match.sourceFile != null) {
                writer.write("\tsource=");
                writer.write(match.sourceFile);
            }
            if (match.methodStartLine >= 0) {
                writer.write("\tmethodStartLine=");
                writer.write(Integer.toString(match.methodStartLine));
            }
            if (match.lineNumber >= 0) {
                writer.write("\tline=");
                writer.write(Integer.toString(match.lineNumber));
            }
            if (match.codeOffset >= 0) {
                writer.write("\toffset=0x");
                writer.write(Integer.toHexString(match.codeOffset));
            }
            if (match.opcode != null) {
                writer.write("\topcode=");
                writer.write(match.opcode);
            }
            writer.write("\treference=");
            writer.write(match.referenceDescriptor);
            if (match.dualReference) {
                writer.write("\tdualReference=true");
            }
            if (match.cryptoRelated) {
                writer.write("\triskCrypto=true");
            }
            if (match.obfuscationRelated) {
                writer.write("\triskObfuscation=true");
            }
            writer.write('\n');
        }

        int index = 1;
        for (CallChain chain : results.callChains) {
            writer.write("CHAIN\tindex=");
            writer.write(Integer.toString(index++));
            writer.write("\tdepth=");
            writer.write(Integer.toString(chain.path.size()));
            writer.write("\tpath=");
            writer.write(chain.toHumanReadablePath());
            writer.write('\n');
        }

        for (DependencyEdge edge : results.dependencyEdges) {
            writer.write("DEP\tfrom=");
            writer.write(edge.fromDescriptor);
            writer.write("\tto=");
            writer.write(edge.toDescriptor);
            writer.write("\tkind=");
            writer.write(edge.kind);
            if (edge.sourceFile != null) {
                writer.write("\tsource=");
                writer.write(edge.sourceFile);
            }
            if (edge.lineNumber >= 0) {
                writer.write("\tline=");
                writer.write(Integer.toString(edge.lineNumber));
            }
            if (edge.opcode != null) {
                writer.write("\topcode=");
                writer.write(edge.opcode);
            }
            if (edge.cryptoRelated) {
                writer.write("\triskCrypto=true");
            }
            if (edge.obfuscationRelated) {
                writer.write("\triskObfuscation=true");
            }
            writer.write('\n');
        }
    }

    private void writeMapping(@Nonnull SearchResults results,
                              @Nullable String mappingNewDescriptor,
                              @Nonnull Writer writer) throws IOException {
        if (mappingNewDescriptor == null) {
            throw new IOException("Missing new descriptor for map output");
        }

        String oldDescriptor = results.target.descriptor;
        Set<String> pairs = new LinkedHashSet<String>();
        addMappingPair(pairs, oldDescriptor, mappingNewDescriptor);

        if (results.target.kind == TargetKind.CLASS) {
            DexFormatter formatter = DexFormatter.INSTANCE;
            for (ClassDef classDef : dexFile.getClasses()) {
                if (!classDef.getType().equals(oldDescriptor)) {
                    continue;
                }
                for (Field field : classDef.getFields()) {
                    String fieldDescriptor = formatter.getFieldDescriptor(field);
                    addMappingPair(pairs, fieldDescriptor,
                            fieldDescriptor.replace(oldDescriptor, mappingNewDescriptor));
                }
                for (Method method : classDef.getMethods()) {
                    String methodDescriptor = formatter.getMethodDescriptor(method);
                    addMappingPair(pairs, methodDescriptor,
                            methodDescriptor.replace(oldDescriptor, mappingNewDescriptor));
                }
                break;
            }

            for (ReferenceMatch match : results.references) {
                if ("string-reference".equals(match.kind)) {
                    continue;
                }
                String reference = match.referenceDescriptor;
                if (reference.indexOf(oldDescriptor) < 0) {
                    continue;
                }
                addMappingPair(pairs, reference, reference.replace(oldDescriptor, mappingNewDescriptor));
            }
        }

        for (String pair : pairs) {
            writer.write(pair);
            writer.write('\n');
        }
    }

    private void addMappingPair(@Nonnull Set<String> pairs,
                                @Nonnull String oldDescriptor,
                                @Nonnull String newDescriptor) {
        pairs.add(oldDescriptor + "," + newDescriptor);
    }

    private void writeJson(@Nonnull SearchResults results, @Nonnull Writer writer) throws IOException {
        int riskRefCount = 0;
        for (ReferenceMatch match : results.references) {
            if (match.cryptoRelated || match.obfuscationRelated) {
                riskRefCount++;
            }
        }

        writer.write("{\n");
        writer.write("  \"target\": {");
        writer.write("\"type\": ");
        writeJsonString(writer, results.target.kind.name().toLowerCase());
        writer.write(", \"descriptor\": ");
        writeJsonString(writer, results.target.descriptor);
        writer.write(", \"owner\": ");
        writeJsonString(writer, results.target.ownerDescriptor);
        writer.write("},\n");

        writer.write("  \"summary\": {");
        writer.write("\"definitions\": ");
        writer.write(Integer.toString(results.definitions.size()));
        writer.write(", \"references\": ");
        writer.write(Integer.toString(results.references.size()));
        writer.write(", \"riskReferences\": ");
        writer.write(Integer.toString(riskRefCount));
        writer.write(", \"callChains\": ");
        writer.write(Integer.toString(results.callChains.size()));
        writer.write(", \"dependencies\": ");
        writer.write(Integer.toString(results.dependencyEdges.size()));
        writer.write("},\n");

        writer.write("  \"chainInput\": [");
        for (int i = 0; i < results.inputChainDescriptors.size(); i++) {
            writeJsonString(writer, results.inputChainDescriptors.get(i));
            if (i != results.inputChainDescriptors.size() - 1) {
                writer.write(", ");
            }
        }
        writer.write("],\n");

        writer.write("  \"definitions\": [\n");
        for (int i = 0; i < results.definitions.size(); i++) {
            DefinitionRecord definition = results.definitions.get(i);
            writer.write("    {");
            writer.write("\"kind\": ");
            writeJsonString(writer, definition.kind);
            writer.write(", \"ownerClass\": ");
            writeJsonString(writer, definition.ownerClass);
            writer.write(", \"descriptor\": ");
            writeJsonString(writer, definition.descriptor);
            writer.write(", \"sourceFile\": ");
            writeJsonStringOrNull(writer, definition.sourceFile);
            writer.write(", \"methodStartLine\": ");
            writeJsonNumberOrNull(writer, definition.methodStartLine);
            writer.write("}");
            if (i != results.definitions.size() - 1) {
                writer.write(',');
            }
            writer.write('\n');
        }
        writer.write("  ],\n");

        writer.write("  \"references\": [\n");
        for (int i = 0; i < results.references.size(); i++) {
            ReferenceMatch match = results.references.get(i);
            writer.write("    {");
            writer.write("\"kind\": ");
            writeJsonString(writer, match.kind);
            writer.write(", \"callerClass\": ");
            writeJsonString(writer, match.callerClass);
            writer.write(", \"callerMethod\": ");
            writeJsonStringOrNull(writer, match.callerMethod);
            writer.write(", \"sourceFile\": ");
            writeJsonStringOrNull(writer, match.sourceFile);
            writer.write(", \"methodStartLine\": ");
            writeJsonNumberOrNull(writer, match.methodStartLine);
            writer.write(", \"line\": ");
            writeJsonNumberOrNull(writer, match.lineNumber);
            writer.write(", \"codeOffset\": ");
            writeJsonNumberOrNull(writer, match.codeOffset);
            writer.write(", \"opcode\": ");
            writeJsonStringOrNull(writer, match.opcode);
            writer.write(", \"reference\": ");
            writeJsonString(writer, match.referenceDescriptor);
            writer.write(", \"dualReference\": ");
            writer.write(match.dualReference ? "true" : "false");
            writer.write(", \"riskCrypto\": ");
            writer.write(match.cryptoRelated ? "true" : "false");
            writer.write(", \"riskObfuscation\": ");
            writer.write(match.obfuscationRelated ? "true" : "false");
            writer.write("}");
            if (i != results.references.size() - 1) {
                writer.write(',');
            }
            writer.write('\n');
        }
        writer.write("  ],\n");

        writer.write("  \"callChains\": [\n");
        for (int i = 0; i < results.callChains.size(); i++) {
            CallChain chain = results.callChains.get(i);
            writer.write("    {\"depth\": ");
            writer.write(Integer.toString(chain.path.size()));
            writer.write(", \"path\": [");
            for (int j = 0; j < chain.path.size(); j++) {
                CallSite callSite = chain.path.get(j);
                writer.write("{");
                writer.write("\"callerClass\": ");
                writeJsonString(writer, callSite.callerClass);
                writer.write(", \"callerMethod\": ");
                writeJsonString(writer, callSite.callerDescriptor);
                writer.write(", \"calleeMethod\": ");
                writeJsonString(writer, callSite.calleeDescriptor);
                writer.write(", \"sourceFile\": ");
                writeJsonStringOrNull(writer, callSite.sourceFile);
                writer.write(", \"methodStartLine\": ");
                writeJsonNumberOrNull(writer, callSite.methodStartLine);
                writer.write(", \"line\": ");
                writeJsonNumberOrNull(writer, callSite.lineNumber);
                writer.write(", \"codeOffset\": ");
                writeJsonNumberOrNull(writer, callSite.codeOffset);
                writer.write(", \"opcode\": ");
                writeJsonString(writer, callSite.opcode);
                writer.write(", \"omitted\": ");
                writer.write(callSite.omitted ? "true" : "false");
                writer.write(", \"omittedReason\": ");
                writeJsonStringOrNull(writer, callSite.omittedReason);
                writer.write(", \"riskCrypto\": ");
                writer.write(callSite.cryptoRelated ? "true" : "false");
                writer.write(", \"riskObfuscation\": ");
                writer.write(callSite.obfuscationRelated ? "true" : "false");
                writer.write("}");
                if (j != chain.path.size() - 1) {
                    writer.write(',');
                }
            }
            writer.write("]}");
            if (i != results.callChains.size() - 1) {
                writer.write(',');
            }
            writer.write('\n');
        }
        writer.write("  ],\n");

        writer.write("  \"dependencies\": [\n");
        for (int i = 0; i < results.dependencyEdges.size(); i++) {
            DependencyEdge edge = results.dependencyEdges.get(i);
            writer.write("    {");
            writer.write("\"from\": ");
            writeJsonString(writer, edge.fromDescriptor);
            writer.write(", \"to\": ");
            writeJsonString(writer, edge.toDescriptor);
            writer.write(", \"kind\": ");
            writeJsonString(writer, edge.kind);
            writer.write(", \"sourceFile\": ");
            writeJsonStringOrNull(writer, edge.sourceFile);
            writer.write(", \"line\": ");
            writeJsonNumberOrNull(writer, edge.lineNumber);
            writer.write(", \"opcode\": ");
            writeJsonStringOrNull(writer, edge.opcode);
            writer.write(", \"riskCrypto\": ");
            writer.write(edge.cryptoRelated ? "true" : "false");
            writer.write(", \"riskObfuscation\": ");
            writer.write(edge.obfuscationRelated ? "true" : "false");
            writer.write("}");
            if (i != results.dependencyEdges.size() - 1) {
                writer.write(',');
            }
            writer.write('\n');
        }
        writer.write("  ]\n");
        writer.write("}\n");
    }

    private void writeJsonString(@Nonnull Writer writer, @Nonnull String value) throws IOException {
        writer.write('"');
        writer.write(StringUtils.escapeString(value));
        writer.write('"');
    }

    private void writeJsonStringOrNull(@Nonnull Writer writer, @Nullable String value) throws IOException {
        if (value == null) {
            writer.write("null");
        } else {
            writeJsonString(writer, value);
        }
    }

    private void writeJsonNumberOrNull(@Nonnull Writer writer, int value) throws IOException {
        if (value < 0) {
            writer.write("null");
        } else {
            writer.write(Integer.toString(value));
        }
    }

    private void addDefinition(@Nonnull List<DefinitionRecord> definitions,
                               @Nonnull Set<String> definitionKeys,
                               @Nonnull DefinitionRecord definition) {
        String key = definition.kind + '|' + definition.ownerClass + '|' + definition.descriptor;
        if (definitionKeys.add(key)) {
            definitions.add(definition);
        }
    }

    @Nonnull
    private ImmutableFieldReference parseFieldDescriptor(@Nonnull String descriptor) {
        String trimmed = descriptor.trim();
        if (trimmed.isEmpty()) {
            throw new IllegalArgumentException("Empty field descriptor");
        }

        int arrow = trimmed.indexOf("->");
        int colon = trimmed.indexOf(':', arrow + 2);
        if (arrow < 0 || colon < 0) {
            throw new IllegalArgumentException("Field descriptor must look like Lpkg/Cls;->name:Type");
        }

        String definingClass = normalizeClassDescriptor(trimmed.substring(0, arrow).trim());
        String name = trimmed.substring(arrow + 2, colon).trim();
        String type = trimmed.substring(colon + 1).trim();
        if (name.isEmpty()) {
            throw new IllegalArgumentException("Missing field name in field descriptor");
        }
        if (type.isEmpty()) {
            throw new IllegalArgumentException("Missing field type in field descriptor");
        }
        return new ImmutableFieldReference(definingClass, name, type);
    }

    @Nonnull
    private String normalizeClassDescriptor(@Nonnull String descriptor) {
        String trimmed = descriptor.trim();
        if (trimmed.isEmpty()) {
            throw new IllegalArgumentException("Empty class descriptor");
        }
        if (trimmed.charAt(0) != 'L' || trimmed.charAt(trimmed.length() - 1) != ';') {
            throw new IllegalArgumentException("Class descriptor must look like Lpkg/Cls;");
        }
        return trimmed;
    }

    private boolean shouldSkipFrameworkClass(@Nonnull String classDescriptor, @Nonnull String targetOwnerDescriptor) {
        if (classDescriptor.equals(targetOwnerDescriptor)) {
            return false;
        }
        for (String prefix : SDK_PREFIXES) {
            if (classDescriptor.startsWith(prefix)) {
                return true;
            }
        }
        return false;
    }

    private boolean isBlank(@Nullable String value) {
        return value == null || value.trim().isEmpty();
    }

    @Nonnull
    private String joinWithArrow(@Nonnull List<String> values) {
        StringBuilder builder = new StringBuilder();
        for (int i = 0; i < values.size(); i++) {
            if (i > 0) {
                builder.append(" => ");
            }
            builder.append(values.get(i));
        }
        return builder.toString();
    }

    private enum TargetKind {
        METHOD,
        FIELD,
        CLASS
    }

    private static final class SearchTarget {
        @Nonnull private final TargetKind kind;
        @Nonnull private final String descriptor;
        @Nonnull private final String ownerDescriptor;

        private SearchTarget(@Nonnull TargetKind kind, @Nonnull String descriptor, @Nonnull String ownerDescriptor) {
            this.kind = kind;
            this.descriptor = descriptor;
            this.ownerDescriptor = ownerDescriptor;
        }
    }

    private static final class SearchResults {
        @Nonnull private final SearchTarget target;
        @Nonnull private final List<DefinitionRecord> definitions = new ArrayList<DefinitionRecord>();
        @Nonnull private final List<ReferenceMatch> references = new ArrayList<ReferenceMatch>();
        @Nonnull private final List<CallChain> callChains = new ArrayList<CallChain>();
        @Nonnull private final List<DependencyEdge> dependencyEdges = new ArrayList<DependencyEdge>();
        @Nonnull private final List<String> inputChainDescriptors = new ArrayList<String>();

        private SearchResults(@Nonnull SearchTarget target) {
            this.target = target;
        }
    }

    private static final class DefinitionRecord {
        @Nonnull private final String kind;
        @Nonnull private final String ownerClass;
        @Nonnull private final String descriptor;
        @Nullable private final String sourceFile;
        private final int methodStartLine;

        private DefinitionRecord(@Nonnull String kind,
                                 @Nonnull String ownerClass,
                                 @Nonnull String descriptor,
                                 @Nullable String sourceFile,
                                 int methodStartLine) {
            this.kind = kind;
            this.ownerClass = ownerClass;
            this.descriptor = descriptor;
            this.sourceFile = sourceFile;
            this.methodStartLine = methodStartLine;
        }
    }

    private static final class ReferenceMatch {
        @Nonnull private final String kind;
        @Nonnull private final String callerClass;
        @Nullable private final String callerMethod;
        @Nullable private final String sourceFile;
        private final int methodStartLine;
        private final int lineNumber;
        private final int codeOffset;
        @Nullable private final String opcode;
        @Nonnull private final String referenceDescriptor;
        private final boolean dualReference;
        private final boolean cryptoRelated;
        private final boolean obfuscationRelated;

        private ReferenceMatch(@Nonnull String kind,
                               @Nonnull String callerClass,
                               @Nullable String callerMethod,
                               @Nullable String sourceFile,
                               int methodStartLine,
                               int lineNumber,
                                int codeOffset,
                                @Nullable String opcode,
                                @Nonnull String referenceDescriptor,
                                boolean dualReference,
                                boolean cryptoRelated,
                                boolean obfuscationRelated) {
            this.kind = kind;
            this.callerClass = callerClass;
            this.callerMethod = callerMethod;
            this.sourceFile = sourceFile;
            this.methodStartLine = methodStartLine;
            this.lineNumber = lineNumber;
            this.codeOffset = codeOffset;
            this.opcode = opcode;
            this.referenceDescriptor = referenceDescriptor;
            this.dualReference = dualReference;
            this.cryptoRelated = cryptoRelated;
            this.obfuscationRelated = obfuscationRelated;
        }
    }

    private static final class CallSite {
        @Nonnull private final String callerClass;
        @Nonnull private final String callerDescriptor;
        @Nullable private final String sourceFile;
        private final int methodStartLine;
        private final int lineNumber;
        private final int codeOffset;
        @Nonnull private final String opcode;
        @Nonnull private final String calleeDescriptor;
        private final boolean omitted;
        @Nullable private final String omittedReason;
        private final boolean cryptoRelated;
        private final boolean obfuscationRelated;

        private CallSite(@Nonnull String callerClass,
                         @Nonnull String callerDescriptor,
                         @Nullable String sourceFile,
                         int methodStartLine,
                         int lineNumber,
                         int codeOffset,
                         @Nonnull String opcode,
                         @Nonnull String calleeDescriptor,
                         boolean omitted,
                         @Nullable String omittedReason,
                         boolean cryptoRelated,
                         boolean obfuscationRelated) {
            this.callerClass = callerClass;
            this.callerDescriptor = callerDescriptor;
            this.sourceFile = sourceFile;
            this.methodStartLine = methodStartLine;
            this.lineNumber = lineNumber;
            this.codeOffset = codeOffset;
            this.opcode = opcode;
            this.calleeDescriptor = calleeDescriptor;
            this.omitted = omitted;
            this.omittedReason = omittedReason;
            this.cryptoRelated = cryptoRelated;
            this.obfuscationRelated = obfuscationRelated;
        }
    }

    private static final class DependencyEdge {
        @Nonnull private final String fromDescriptor;
        @Nonnull private final String toDescriptor;
        @Nonnull private final String kind;
        @Nullable private final String sourceFile;
        private final int lineNumber;
        @Nullable private final String opcode;
        private final boolean cryptoRelated;
        private final boolean obfuscationRelated;

        private DependencyEdge(@Nonnull String fromDescriptor,
                               @Nonnull String toDescriptor,
                               @Nonnull String kind,
                               @Nullable String sourceFile,
                               int lineNumber,
                               @Nullable String opcode,
                               boolean cryptoRelated,
                               boolean obfuscationRelated) {
            this.fromDescriptor = fromDescriptor;
            this.toDescriptor = toDescriptor;
            this.kind = kind;
            this.sourceFile = sourceFile;
            this.lineNumber = lineNumber;
            this.opcode = opcode;
            this.cryptoRelated = cryptoRelated;
            this.obfuscationRelated = obfuscationRelated;
        }
    }

    private static final class RiskTag {
        private final boolean cryptoRelated;
        private final boolean obfuscationRelated;

        private RiskTag(boolean cryptoRelated, boolean obfuscationRelated) {
            this.cryptoRelated = cryptoRelated;
            this.obfuscationRelated = obfuscationRelated;
        }

        private boolean hasRisk() {
            return cryptoRelated || obfuscationRelated;
        }
    }

    private static final class CallChain {
        @Nonnull private final List<CallSite> path;

        private CallChain(@Nonnull List<CallSite> path) {
            this.path = path;
        }

        @Nonnull
        private static CallChain fromNearestFirstPath(@Nonnull Deque<CallSite> path) {
            List<CallSite> ordered = new ArrayList<CallSite>(path);
            Collections.reverse(ordered);
            return new CallChain(ordered);
        }

        @Nonnull
        private String toKey() {
            StringBuilder builder = new StringBuilder();
            for (CallSite callSite : path) {
                if (builder.length() > 0) {
                    builder.append('>');
                }
                builder.append(callSite.callerDescriptor)
                        .append('@')
                        .append(callSite.codeOffset)
                        .append("->")
                        .append(callSite.calleeDescriptor);
            }
            return builder.toString();
        }

        @Nonnull
        private String toHumanReadablePath() {
            StringBuilder builder = new StringBuilder();
            for (int i = 0; i < path.size(); i++) {
                CallSite callSite = path.get(i);
                if (i > 0) {
                    builder.append(" => ");
                }
                builder.append(callSite.callerDescriptor)
                        .append(" [opcode=")
                        .append(callSite.opcode)
                        .append(", offset=0x")
                        .append(Integer.toHexString(callSite.codeOffset));
                if (callSite.lineNumber >= 0) {
                    builder.append(", line=").append(callSite.lineNumber);
                }
                builder.append("]");
            }
            if (!path.isEmpty()) {
                builder.append(" => ").append(path.get(path.size() - 1).calleeDescriptor);
            }
            return builder.toString();
        }
    }

    private static final class SavePaths {
        @Nonnull private final File baseDir;
        @Nullable private final File registryFile;
        @Nonnull private final File mosuyLogFile;
        @Nonnull private final File resultCacheFile;

        private SavePaths(@Nonnull File baseDir,
                          @Nullable File registryFile,
                          @Nonnull File mosuyLogFile,
                          @Nonnull File resultCacheFile) {
            this.baseDir = baseDir;
            this.registryFile = registryFile;
            this.mosuyLogFile = mosuyLogFile;
            this.resultCacheFile = resultCacheFile;
        }
    }

    private static final class LogSink {
        @Nonnull private final Writer writer;

        private LogSink(@Nonnull Writer writer) {
            this.writer = writer;
        }

        @Nullable
        private static LogSink open(@Nullable File file) {
            if (file == null) {
                return null;
            }
            File parent = file.getAbsoluteFile().getParentFile();
            if (parent != null && !parent.exists() && !parent.mkdirs()) {
                System.err.println("Unable to create log directory: " + parent);
                return null;
            }
            try {
                return new LogSink(new BufferedWriter(new OutputStreamWriter(
                        new FileOutputStream(file, true), StandardCharsets.UTF_8)));
            } catch (IOException ex) {
                System.err.println("Unable to open log file: " + file + ": " + ex.getMessage());
                return null;
            }
        }

        private void write(@Nonnull String category, @Nonnull String message) {
            try {
                writer.write('[');
                writer.write(category);
                writer.write("] ");
                writer.write(message);
                writer.write('\n');
                writer.flush();
            } catch (IOException ex) {
                // ignore logging failures
            }
        }

        private void close() {
            try {
                writer.close();
            } catch (IOException ex) {
                // ignore
            }
        }
    }

    private static final class ResultCacheStore {
        private static final String MAGIC = "#xref-cache-v1";

        @Nonnull private final File file;
        @Nonnull private final LinkedHashMap<String, String> entries = new LinkedHashMap<String, String>();

        private ResultCacheStore(@Nonnull File file) {
            this.file = file;
        }

        @Nonnull
        private static ResultCacheStore open(@Nonnull File file) {
            ResultCacheStore store = new ResultCacheStore(file);
            store.load();
            return store;
        }

        @Nullable
        private String get(@Nonnull String key) {
            return entries.get(key);
        }

        private void put(@Nonnull String key, @Nonnull String renderedOutput, int maxEntries) {
            entries.remove(key);
            entries.put(key, renderedOutput);
            while (entries.size() > maxEntries) {
                String oldest = entries.keySet().iterator().next();
                entries.remove(oldest);
            }
        }

        private void load() {
            if (!file.isFile()) {
                return;
            }

            BufferedReader reader = null;
            try {
                reader = new BufferedReader(new InputStreamReader(new FileInputStream(file), StandardCharsets.UTF_8));
                String line;
                boolean checkedHeader = false;
                while ((line = reader.readLine()) != null) {
                    if (!checkedHeader) {
                        checkedHeader = true;
                        if (!MAGIC.equals(line)) {
                            return;
                        }
                        continue;
                    }
                    if (line.trim().isEmpty()) {
                        continue;
                    }
                    int split = line.indexOf('\t');
                    if (split <= 0 || split >= line.length() - 1) {
                        continue;
                    }
                    String keyPart = line.substring(0, split);
                    String valuePart = line.substring(split + 1);
                    try {
                        String key = new String(Base64.getDecoder().decode(keyPart), StandardCharsets.UTF_8);
                        String value = new String(Base64.getDecoder().decode(valuePart), StandardCharsets.UTF_8);
                        entries.put(key, value);
                    } catch (IllegalArgumentException ex) {
                        // ignore malformed cache lines
                    }
                }
            } catch (IOException ex) {
                // ignore cache read failures
            } finally {
                if (reader != null) {
                    try {
                        reader.close();
                    } catch (IOException ex) {
                        // ignore
                    }
                }
            }
        }

        private void save() {
            File parent = file.getAbsoluteFile().getParentFile();
            if (parent != null && !parent.exists() && !parent.mkdirs()) {
                return;
            }

            Writer writer = null;
            try {
                writer = new BufferedWriter(new OutputStreamWriter(new FileOutputStream(file), StandardCharsets.UTF_8));
                writer.write(MAGIC);
                writer.write('\n');
                for (Map.Entry<String, String> entry : entries.entrySet()) {
                    String key = Base64.getEncoder().encodeToString(entry.getKey().getBytes(StandardCharsets.UTF_8));
                    String value = Base64.getEncoder().encodeToString(entry.getValue().getBytes(StandardCharsets.UTF_8));
                    writer.write(key);
                    writer.write('\t');
                    writer.write(value);
                    writer.write('\n');
                }
                writer.flush();
            } catch (IOException ex) {
                // ignore cache write failures
            } finally {
                if (writer != null) {
                    try {
                        writer.close();
                    } catch (IOException ex) {
                        // ignore
                    }
                }
            }
        }
    }

    private static final class ComponentRegistry {
        @Nonnull private final Set<String> packagePrefixes;
        @Nonnull private final Set<String> classDescriptors;
        @Nonnull private final Set<String> methodDescriptors;

        private ComponentRegistry(@Nonnull Set<String> packagePrefixes,
                                  @Nonnull Set<String> classDescriptors,
                                  @Nonnull Set<String> methodDescriptors) {
            this.packagePrefixes = packagePrefixes;
            this.classDescriptors = classDescriptors;
            this.methodDescriptors = methodDescriptors;
        }

        @Nonnull
        private static ComponentRegistry empty() {
            return new ComponentRegistry(Collections.<String>emptySet(),
                    Collections.<String>emptySet(),
                    Collections.<String>emptySet());
        }

        @Nonnull
        private static LoadResult load(@Nonnull File file) {
            List<String> errors = new ArrayList<String>();
            if (!file.exists()) {
                errors.add("registry file not found: " + file.getAbsolutePath());
                return new LoadResult(empty(), errors);
            }

            Set<String> packagePrefixes = new LinkedHashSet<String>();
            Set<String> classDescriptors = new LinkedHashSet<String>();
            Set<String> methodDescriptors = new LinkedHashSet<String>();

            BufferedReader reader = null;
            try {
                reader = new BufferedReader(new InputStreamReader(
                        new FileInputStream(file), StandardCharsets.UTF_8));
                String line;
                int lineNumber = 0;
                while ((line = reader.readLine()) != null) {
                    lineNumber++;
                    String trimmed = line.trim();
                    if (trimmed.isEmpty() || trimmed.startsWith("#") || trimmed.startsWith("//")) {
                        continue;
                    }

                    String kind = null;
                    String value = trimmed;
                    int colon = trimmed.indexOf(':');
                    if (colon > 0) {
                        String prefix = trimmed.substring(0, colon).trim().toLowerCase();
                        if (prefix.equals("p") || prefix.equals("package") || prefix.equals("pkg")) {
                            kind = "package";
                            value = trimmed.substring(colon + 1).trim();
                        } else if (prefix.equals("c") || prefix.equals("class")) {
                            kind = "class";
                            value = trimmed.substring(colon + 1).trim();
                        } else if (prefix.equals("m") || prefix.equals("method")) {
                            kind = "method";
                            value = trimmed.substring(colon + 1).trim();
                        }
                    }

                    if (kind == null) {
                        if (value.contains("->")) {
                            kind = "method";
                        } else if (value.endsWith(";")) {
                            kind = "class";
                        } else {
                            kind = "package";
                        }
                    }

                    if (kind.equals("method")) {
                        String normalized = normalizeMethodDescriptor(value, lineNumber, errors);
                        if (normalized != null) {
                            methodDescriptors.add(normalized);
                        }
                        continue;
                    }
                    if (kind.equals("class")) {
                        String normalized = normalizeClassDescriptor(value, lineNumber, errors);
                        if (normalized != null) {
                            classDescriptors.add(normalized);
                        }
                        continue;
                    }

                    String normalized = normalizePackagePrefix(value, lineNumber, errors);
                    if (normalized != null) {
                        packagePrefixes.add(normalized);
                    }
                }
            } catch (IOException ex) {
                errors.add("failed to read registry file: " + ex.getMessage());
            } finally {
                if (reader != null) {
                    try {
                        reader.close();
                    } catch (IOException ex) {
                        // ignore
                    }
                }
            }

            return new LoadResult(new ComponentRegistry(packagePrefixes, classDescriptors, methodDescriptors), errors);
        }

        private boolean matchesMethod(@Nonnull String methodDescriptor, @Nonnull String classDescriptor) {
            if (methodDescriptors.contains(methodDescriptor)) {
                return true;
            }
            return matchesClass(classDescriptor);
        }

        private boolean matchesClass(@Nonnull String classDescriptor) {
            if (classDescriptors.contains(classDescriptor)) {
                return true;
            }
            for (String prefix : packagePrefixes) {
                if (classDescriptor.startsWith(prefix)) {
                    return true;
                }
            }
            return false;
        }

        @Nullable
        private static String normalizeMethodDescriptor(@Nonnull String value, int lineNumber,
                                                        @Nonnull List<String> errors) {
            try {
                ImmutableMethodReference ref = MethodDescriptorParser.parse(value);
                return DexFormatter.INSTANCE.getMethodDescriptor(ref);
            } catch (IllegalArgumentException ex) {
                errors.add("line " + lineNumber + ": invalid method entry: " + value + " (" + ex.getMessage() + ")");
                return null;
            }
        }

        @Nullable
        private static String normalizeClassDescriptor(@Nonnull String value, int lineNumber,
                                                       @Nonnull List<String> errors) {
            String trimmed = value.trim();
            if (trimmed.isEmpty()) {
                errors.add("line " + lineNumber + ": empty class entry");
                return null;
            }
            if (!trimmed.startsWith("L")) {
                trimmed = "L" + trimmed;
            }
            if (!trimmed.endsWith(";")) {
                trimmed = trimmed + ";";
            }
            if (!trimmed.endsWith(";")) {
                errors.add("line " + lineNumber + ": invalid class entry: " + value);
                return null;
            }
            return trimmed;
        }

        @Nullable
        private static String normalizePackagePrefix(@Nonnull String value, int lineNumber,
                                                     @Nonnull List<String> errors) {
            String trimmed = value.trim();
            if (trimmed.isEmpty()) {
                errors.add("line " + lineNumber + ": empty package entry");
                return null;
            }
            if (trimmed.endsWith("*")) {
                trimmed = trimmed.substring(0, trimmed.length() - 1);
            }
            if (!trimmed.startsWith("L")) {
                trimmed = "L" + trimmed;
            }
            if (trimmed.endsWith(";")) {
                trimmed = trimmed.substring(0, trimmed.length() - 1);
            }
            if (!trimmed.endsWith("/")) {
                trimmed = trimmed + "/";
            }
            return trimmed;
        }

        private static final class LoadResult {
            @Nonnull private final ComponentRegistry registry;
            @Nonnull private final List<String> errors;

            private LoadResult(@Nonnull ComponentRegistry registry, @Nonnull List<String> errors) {
                this.registry = registry;
                this.errors = errors;
            }

            @Nonnull
            private ComponentRegistry getRegistry() {
                return registry;
            }

            @Nonnull
            private List<String> getErrors() {
                return errors;
            }
        }
    }

    private static final class DebugLineResolver {
        @Nonnull private final List<LineEntry> lineEntries = new ArrayList<LineEntry>();
        private int methodStartLine = -1;

        private DebugLineResolver(@Nullable MethodImplementation methodImplementation) {
            if (methodImplementation == null) {
                return;
            }

            for (DebugItem debugItem : methodImplementation.getDebugItems()) {
                if (debugItem instanceof LineNumber) {
                    int lineNumber = ((LineNumber) debugItem).getLineNumber();
                    lineEntries.add(new LineEntry(debugItem.getCodeAddress(), lineNumber));
                    if (methodStartLine < 0) {
                        methodStartLine = lineNumber;
                    }
                }
            }
        }

        private int getMethodStartLine() {
            return methodStartLine;
        }

        private int getLineNumber(int codeAddress) {
            int resolved = -1;
            for (LineEntry lineEntry : lineEntries) {
                if (lineEntry.codeAddress > codeAddress) {
                    break;
                }
                resolved = lineEntry.lineNumber;
            }
            return resolved;
        }
    }

    private static final class LineEntry {
        private final int codeAddress;
        private final int lineNumber;

        private LineEntry(int codeAddress, int lineNumber) {
            this.codeAddress = codeAddress;
            this.lineNumber = lineNumber;
        }
    }
}






























