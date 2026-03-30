package org.jf.baksmali.sposkosm.tools;


import org.jf.dexlib2.iface.ClassDef;
import org.jf.dexlib2.iface.Method;
import org.jf.dexlib2.iface.reference.TypeReference;

import java.util.Objects;

public class MutableClassDef implements Comparable<String> {

    private final MutableDexFile parentDex;
    private final ClassDef classDef;

    public MutableClassDef(MutableDexFile parentDex, ClassDef classDef) {
        this.parentDex = parentDex;
        this.classDef = classDef;
    }

    public int getAccessFlags() {
        return classDef.getAccessFlags();
    }

    public String getType() {
        return classDef.getType();
    }

    public Iterable<Method> getMethods() {
        return (Iterable<Method>) classDef.getMethods();
    }

    public MutableDexFile getParentDex() {
        return parentDex;
    }

    public ClassDef getClassDef() {
        return classDef;
    }

    /**
     * See TypeReference.equals
     */
    @Override
    public boolean equals(Object other) {
        if (this == other) return true;
        if (!(other instanceof MutableClassDef)) return false;

        MutableClassDef that = (MutableClassDef) other;
        return classDef.toString().equals(that.toString());
    }

    /**
     * See TypeReference.hashCode
     */
    @Override
    public int hashCode() {
        return classDef.hashCode();
    }

    /**
     * See TypeReference.compareTo
     */
    @Override
    public int compareTo(String other) {
        return classDef.compareTo(other);
    }

    /**
     * See TypeReference.toString
     */
    @Override
    public String toString() {
        return classDef.toString();
    }

    // 添加一些实用方法
    public boolean isInterface() {
        return (getAccessFlags() & org.jf.dexlib2.AccessFlags.INTERFACE.getValue()) != 0;
    }

    public boolean isEnum() {
        return (getAccessFlags() & org.jf.dexlib2.AccessFlags.ENUM.getValue()) != 0;
    }

    public boolean isAnnotation() {
        return (getAccessFlags() & org.jf.dexlib2.AccessFlags.ANNOTATION.getValue()) != 0;
    }
}