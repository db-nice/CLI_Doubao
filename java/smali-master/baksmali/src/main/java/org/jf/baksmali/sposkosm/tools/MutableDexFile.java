package org.jf.baksmali.sposkosm.tools;


import org.jf.dexlib2.Opcodes;
import org.jf.dexlib2.dexbacked.DexBackedDexFile;
import org.jf.dexlib2.iface.ClassDef;
import org.jf.dexlib2.util.DexUtil;
import org.jf.dexlib2.writer.io.FileDataStore;
import org.jf.dexlib2.writer.pool.DexPool;

import java.io.File;
import java.io.IOException;
import java.util.ArrayList;
import java.util.List;
import java.util.Objects;
import java.util.stream.Collectors;

public class MutableDexFile {
    private static final int DEFAULT_DEX_VERSION = 35;

    private final int dexVersion;
    private final List<MutableClassDef> _classes;

    private final File dexFile;
    private final Opcodes opcodes;

    public MutableDexFile(File dexFile, byte[] byteArray) {
        this.dexFile = dexFile;
        this.dexVersion = DexUtil.verifyDexHeader(byteArray, 0);
        this.opcodes = Opcodes.forDexVersion(this.dexVersion);

        DexBackedDexFile dexBacked = new DexBackedDexFile(this.opcodes, byteArray);
        this._classes = dexBacked.getClasses().stream()
                .map(classDef -> new MutableClassDef(this, classDef))
                .collect(Collectors.toCollection(ArrayList::new));
    }

    public MutableDexFile() {
        this(new ArrayList<>(), null);
    }

    public MutableDexFile(List<MutableClassDef> classDefs) {
        this(classDefs, null);
    }

    public MutableDexFile(List<MutableClassDef> classDefs, Integer dexVersion) {
        this.dexFile = null;

        if (dexVersion != null) {
            this.dexVersion = dexVersion;
        } else {
            // Find the maximum dex version from classDefs, or use default
            this.dexVersion = classDefs.stream()
                    .mapToInt(def -> def.getParentDex().getDexVersion())
                    .max()
                    .orElse(DEFAULT_DEX_VERSION);
        }

        this.opcodes = Opcodes.forDexVersion(this.dexVersion);
        this._classes = new ArrayList<>(classDefs);
    }

    public File getDexFile() {
        return dexFile;
    }

    public Opcodes getOpcodes() {
        return opcodes;
    }

    public List<MutableClassDef> getClasses() {
        return new ArrayList<>(_classes); // Return a copy to prevent external modification
    }

    public int getDexVersion() {
        return dexVersion;
    }

    /**
     * Adds a ClassDef if it doesn't exist.
     */
    public void addClassDef(ClassDef classDef) {
        MutableClassDef def = new MutableClassDef(this, classDef);

        if (!_classes.contains(def)) {
            _classes.add(def);
        }
    }

    /**
     * Replaces a ClassDef if it exists.
     */
    public void replaceClassDef(ClassDef classDef) {
        MutableClassDef def = new MutableClassDef(this, classDef);
        int index = _classes.indexOf(def);

        if (index != -1) {
            _classes.set(index, def);
        }
    }

    /**
     * Finds MutableClassDef from the classDescriptor specified,
     * returns null if no match is found.
     */
    public MutableClassDef findClassDef(String classDescriptor) {
        if (classDescriptor == null) {
            return null;
        }

        for (MutableClassDef classDef : _classes) {
            if (classDescriptor.equals(classDef.getType())) {
                return classDef;
            }
        }
        return null;
    }

    /**
     * Deletes a ClassDef if it exists.
     */
    public boolean deleteClassDef(String classDescriptor) {
        MutableClassDef element = findClassDef(classDescriptor);
        if (element != null) {
            return deleteClassDef(element);
        }
        return false;
    }

    /**
     * Deletes a ClassDef if it exists.
     */
    public boolean deleteClassDef(MutableClassDef classDef) {
        return _classes.remove(classDef);
    }

    /**
     * Deletes MutableClassDefs in the given package.
     *
     * packagePath being in somePackage/someClass format.
     */
    public void deletePackage(String packagePath) {
        String prefix = "L" + packagePath + "/";
        _classes.removeIf(classDef -> classDef.getType().startsWith(prefix));
    }

    /**
     * Writes to the file specified.
     */
    public void writeToFile(File file) throws IOException {
        DexPool dexPool = new DexPool(opcodes);
        for (MutableClassDef classDef : _classes) {
            dexPool.internClass(classDef.getClassDef());
        }
        dexPool.writeTo(new FileDataStore(file));
    }

    @Override
    public boolean equals(Object o) {
        if (this == o) return true;
        if (o == null || getClass() != o.getClass()) return false;
        MutableDexFile that = (MutableDexFile) o;
        return dexVersion == that.dexVersion &&
                Objects.equals(dexFile, that.dexFile) &&
                Objects.equals(_classes, that._classes);
    }

    @Override
    public int hashCode() {
        return Objects.hash(dexVersion, dexFile, _classes);
    }

    @Override
    public String toString() {
        return "MutableDexFile{" +
                "dexVersion=" + dexVersion +
                ", dexFile=" + dexFile +
                ", classesCount=" + _classes.size() +
                '}';
    }
}
